# Authors: Karl MacMillan <kmacmillan@mentalrootkit.com>
#
# Copyright (C) 2007  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import sys
import os
import pwd
import socket
import datetime
import traceback
import tempfile

import six

from ipalib.install import certstore, sysrestore
from ipapython import ipautil
from ipapython.dn import DN
from ipapython.ipa_log_manager import root_logger
from ipapython import kerberos
from ipalib import api, errors
from ipaplatform import services
from ipaplatform.paths import paths


if six.PY3:
    unicode = str

# The service name as stored in cn=masters,cn=ipa,cn=etc. In the tuple
# the first value is the *nix service name, the second the start order.
SERVICE_LIST = {
    'KDC': ('krb5kdc', 10),
    'KPASSWD': ('kadmin', 20),
    'DNS': ('named', 30),
    'MEMCACHE': ('ipa_memcached', 39),
    'HTTP': ('httpd', 40),
    'KEYS': ('ipa-custodia', 41),
    'NTP': ('ntpd', 45),
    'CA': ('pki-tomcatd', 50),
    'KRA': ('pki-tomcatd', 51),
    'ADTRUST': ('smb', 60),
    'EXTID': ('winbind', 70),
    'OTPD': ('ipa-otpd', 80),
    'DNSKeyExporter': ('ipa-ods-exporter', 90),
    'DNSSEC': ('ods-enforcerd', 100),
    'DNSKeySync': ('ipa-dnskeysyncd', 110),
}

def print_msg(message, output_fd=sys.stdout):
    root_logger.debug(message)
    output_fd.write(message)
    output_fd.write("\n")
    output_fd.flush()


def format_seconds(seconds):
    """Format a number of seconds as an English minutes+seconds message"""
    parts = []
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        parts.append('%d minute' % minutes)
        if minutes != 1:
            parts[-1] += 's'
    if seconds or not minutes:
        parts.append('%d second' % seconds)
        if seconds != 1:
            parts[-1] += 's'
    return ' '.join(parts)

def add_principals_to_group(admin_conn, group, member_attr, principals):
    """Add principals to a GroupOfNames LDAP group
    admin_conn  -- LDAP connection with admin rights
    group       -- DN of the group
    member_attr -- attribute to represent members
    principals  -- list of DNs to add as members
    """
    try:
        current = admin_conn.get_entry(group)
        members = current.get(member_attr, [])
        if len(members) == 0:
            current[member_attr] = []
        for amember in principals:
            if not(amember in members):
                current[member_attr].extend([amember])
        admin_conn.update_entry(current)
    except errors.NotFound:
        entry = admin_conn.make_entry(
                group,
                objectclass=["top", "GroupOfNames"],
                cn=[group['cn']],
                member=principals,
        )
        admin_conn.add_entry(entry)
    except errors.EmptyModlist:
        # If there are no changes just pass
        pass


def find_providing_server(svcname, conn, host_name=None, api=api):
    """
    :param svcname: The service to find
    :param conn: a connection to the LDAP server
    :param host_name: the preferred server
    :return: the selected host name

    Find a server that is a CA.
    """
    dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), api.env.basedn)
    query_filter = conn.make_filter({'objectClass': 'ipaConfigObject',
                                     'ipaConfigString': 'enabledService',
                                     'cn': svcname}, rules='&')
    try:
        entries, _trunc = conn.find_entries(filter=query_filter, base_dn=dn)
    except errors.NotFound:
        return None
    if len(entries):
        if host_name is not None:
            for entry in entries:
                if entry.dn[1].value == host_name:
                    return host_name
        # if the preferred is not found, return the first in the list
        return entries[0].dn[1].value
    return None


class Service(object):
    def __init__(self, service_name, service_desc=None, sstore=None,
                 fstore=None, api=api, realm_name=None,
                 service_user=None, service_prefix=None,
                 keytab=None):
        self.service_name = service_name
        self.service_desc = service_desc
        self.service = services.service(service_name, api)
        self.steps = []
        self.output_fd = sys.stdout

        self.fqdn = socket.gethostname()

        if sstore:
            self.sstore = sstore
        else:
            self.sstore = sysrestore.StateFile(paths.SYSRESTORE)

        if fstore:
            self.fstore = fstore
        else:
            self.fstore = sysrestore.FileStore(paths.SYSRESTORE)

        self.realm = realm_name
        self.suffix = DN()
        self.service_prefix = service_prefix
        self.keytab = keytab
        self.dercert = None
        self.api = api
        self.service_user = service_user
        self.dm_password = None  # silence pylint
        self.promote = False

    @property
    def principal(self):
        if any(attr is None for attr in (self.realm, self.fqdn,
                                         self.service_prefix)):
            return

        return unicode(
            kerberos.Principal(
                (self.service_prefix, self.fqdn), realm=self.realm))

    def _ldap_mod(self, ldif, sub_dict=None, raise_on_err=True,
                  ldap_uri=None, dm_password=None):
        pw_name = None
        fd = None
        path = os.path.join(paths.USR_SHARE_IPA_DIR, ldif)
        nologlist = []

        if sub_dict is not None:
            txt = ipautil.template_file(path, sub_dict)
            fd = ipautil.write_tmp_file(txt)
            path = fd.name

            # do not log passwords
            if 'PASSWORD' in sub_dict:
                nologlist.append(sub_dict['PASSWORD'])
            if 'RANDOM_PASSWORD' in sub_dict:
                nologlist.append(sub_dict['RANDOM_PASSWORD'])

        args = [paths.LDAPMODIFY, "-v", "-f", path]

        # As we always connect to the local host,
        # use URI of admin connection
        if not ldap_uri:
            ldap_uri = api.Backend.ldap2.ldap_uri

        args += ["-H", ldap_uri]

        if dm_password:
            [pw_fd, pw_name] = tempfile.mkstemp()
            os.write(pw_fd, dm_password)
            os.close(pw_fd)
            auth_parms = ["-x", "-D", "cn=Directory Manager", "-y", pw_name]
        # Use GSSAPI auth when not using DM password or not being root
        elif os.getegid() != 0:
            auth_parms = ["-Y", "GSSAPI"]
        # Default to EXTERNAL auth mechanism
        else:
            auth_parms = ["-Y", "EXTERNAL"]

        args += auth_parms

        try:
            try:
                ipautil.run(args, nolog=nologlist)
            except ipautil.CalledProcessError as e:
                root_logger.critical("Failed to load %s: %s" % (ldif, str(e)))
                if raise_on_err:
                    raise
        finally:
            if pw_name:
                os.remove(pw_name)

    def move_service(self, principal):
        """
        Used to move a principal entry created by kadmin.local from
        cn=kerberos to cn=services
        """

        dn = DN(('krbprincipalname', principal), ('cn', self.realm), ('cn', 'kerberos'), self.suffix)
        try:
            entry = api.Backend.ldap2.get_entry(dn)
        except errors.NotFound:
            # There is no service in the wrong location, nothing to do.
            # This can happen when installing a replica
            return None
        entry.pop('krbpwdpolicyreference', None)  # don't copy virtual attr
        newdn = DN(('krbprincipalname', principal), ('cn', 'services'), ('cn', 'accounts'), self.suffix)
        hostdn = DN(('fqdn', self.fqdn), ('cn', 'computers'), ('cn', 'accounts'), self.suffix)
        api.Backend.ldap2.delete_entry(entry)
        entry.dn = newdn
        classes = entry.get("objectclass")
        classes = classes + ["ipaobject", "ipaservice", "pkiuser"]
        entry["objectclass"] = list(set(classes))
        entry["ipauniqueid"] = ['autogenerate']
        entry["managedby"] = [hostdn]
        api.Backend.ldap2.add_entry(entry)
        return newdn

    def add_simple_service(self, principal):
        """
        Add a very basic IPA service.

        The principal needs to be fully-formed: service/host@REALM
        """
        dn = DN(('krbprincipalname', principal), ('cn', 'services'), ('cn', 'accounts'), self.suffix)
        hostdn = DN(('fqdn', self.fqdn), ('cn', 'computers'), ('cn', 'accounts'), self.suffix)
        entry = api.Backend.ldap2.make_entry(
            dn,
            objectclass=[
                "krbprincipal", "krbprincipalaux", "krbticketpolicyaux",
                "ipaobject", "ipaservice", "pkiuser"],
            krbprincipalname=[principal],
            ipauniqueid=['autogenerate'],
            managedby=[hostdn],
        )
        api.Backend.ldap2.add_entry(entry)
        return dn

    def add_cert_to_service(self):
        """
        Add a certificate to a service

        This server cert should be in DER format.
        """
        dn = DN(('krbprincipalname', self.principal), ('cn', 'services'),
                ('cn', 'accounts'), self.suffix)
        entry = api.Backend.ldap2.get_entry(dn)
        entry.setdefault('userCertificate', []).append(self.dercert)
        try:
            api.Backend.ldap2.update_entry(entry)
        except Exception as e:
            root_logger.critical("Could not add certificate to service %s entry: %s" % (self.principal, str(e)))

    def import_ca_certs(self, db, ca_is_configured, conn=None):
        if conn is None:
            conn = api.Backend.ldap2

        try:
            ca_certs = certstore.get_ca_certs_nss(
                conn, self.suffix, self.realm, ca_is_configured)
        except errors.NotFound:
            pass
        else:
            for cert, nickname, trust_flags in ca_certs:
                db.add_cert(cert, nickname, trust_flags)

    def is_configured(self):
        return self.sstore.has_state(self.service_name)

    def set_output(self, fd):
        self.output_fd = fd

    def stop(self, instance_name="", capture_output=True):
        self.service.stop(instance_name, capture_output=capture_output)

    def start(self, instance_name="", capture_output=True, wait=True):
        self.service.start(instance_name, capture_output=capture_output, wait=wait)

    def restart(self, instance_name="", capture_output=True, wait=True):
        self.service.restart(instance_name, capture_output=capture_output, wait=wait)

    def is_running(self, instance_name="", wait=True):
        return self.service.is_running(instance_name, wait)

    def install(self):
        self.service.install()

    def remove(self):
        self.service.remove()

    def enable(self):
        self.service.enable()

    def disable(self):
        self.service.disable()

    def is_enabled(self):
        return self.service.is_enabled()

    def mask(self):
        return self.service.mask()

    def unmask(self):
        return self.service.unmask()

    def is_masked(self):
        return self.service.is_masked()

    def backup_state(self, key, value):
        self.sstore.backup_state(self.service_name, key, value)

    def restore_state(self, key):
        return self.sstore.restore_state(self.service_name, key)

    def get_state(self, key):
        return self.sstore.get_state(self.service_name, key)

    def print_msg(self, message):
        print_msg(message, self.output_fd)

    def step(self, message, method, run_after_failure=False):
        self.steps.append((message, method, run_after_failure))

    def start_creation(self, start_message=None, end_message=None,
        show_service_name=True, runtime=-1):
        """
        Starts creation of the service.

        Use start_message and end_message for explicit messages
        at the beggining / end of the process. Otherwise they are generated
        using the service description (or service name, if the description has
        not been provided).

        Use show_service_name to include service name in generated descriptions.
        """

        if start_message is None:
            # no other info than mandatory service_name provided, use that
            if self.service_desc is None:
                start_message = "Configuring %s" % self.service_name

            # description should be more accurate than service name
            else:
                start_message = "Configuring %s" % self.service_desc
                if show_service_name:
                    start_message = "%s (%s)" % (start_message, self.service_name)

        if end_message is None:
            if self.service_desc is None:
                if show_service_name:
                    end_message = "Done configuring %s." % self.service_name
                else:
                    end_message = "Done."
            else:
                if show_service_name:
                    end_message = "Done configuring %s (%s)." % (
                        self.service_desc, self.service_name)
                else:
                    end_message = "Done configuring %s." % self.service_desc

        if runtime > 0:
            self.print_msg('%s. Estimated time: %s' % (start_message,
                                                      format_seconds(runtime)))
        else:
            self.print_msg(start_message)

        def run_step(message, method):
            self.print_msg(message)
            s = datetime.datetime.now()
            method()
            e = datetime.datetime.now()
            d = e - s
            root_logger.debug("  duration: %d seconds" % d.seconds)

        step = 0
        steps_iter = iter(self.steps)
        try:
            for message, method, run_after_failure in steps_iter:
                full_msg = "  [%d/%d]: %s" % (step+1, len(self.steps), message)
                run_step(full_msg, method)
                step += 1
        except BaseException as e:
            if not (isinstance(e, SystemExit) and
                    e.code == 0):  # pylint: disable=no-member
                # show the traceback, so it's not lost if cleanup method fails
                root_logger.debug("%s" % traceback.format_exc())
                self.print_msg('  [error] %s: %s' % (type(e).__name__, e))

                # run through remaining methods marked run_after_failure
                for message, method, run_after_failure in steps_iter:
                    if run_after_failure:
                        run_step("  [cleanup]: %s" % message, method)

            raise

        self.print_msg(end_message)

        self.steps = []

    def ldap_enable(self, name, fqdn, dm_password=None, ldap_suffix='',
                    config=[]):
        assert isinstance(ldap_suffix, DN)
        self.disable()

        entry_name = DN(('cn', name), ('cn', fqdn), ('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'), ldap_suffix)

        # enable disabled service
        try:
            entry = api.Backend.ldap2.get_entry(
                entry_name, ['ipaConfigString'])
        except errors.NotFound:
            pass
        else:
            if any(u'enabledservice' == val.lower()
                   for val in entry.get('ipaConfigString', [])):
                root_logger.debug("service %s startup entry already enabled", name)
                return

            entry.setdefault('ipaConfigString', []).append(u'enabledService')

            try:
                api.Backend.ldap2.update_entry(entry)
            except errors.EmptyModlist:
                root_logger.debug("service %s startup entry already enabled", name)
                return
            except:
                root_logger.debug("failed to enable service %s startup entry", name)
                raise

            root_logger.debug("service %s startup entry enabled", name)
            return

        order = SERVICE_LIST[name][1]
        entry = api.Backend.ldap2.make_entry(
            entry_name,
            objectclass=["nsContainer", "ipaConfigObject"],
            cn=[name],
            ipaconfigstring=[
                "enabledService", "startOrder " + str(order)] + config,
        )

        try:
            api.Backend.ldap2.add_entry(entry)
        except (errors.DuplicateEntry) as e:
            root_logger.debug("failed to add service %s startup entry", name)
            raise e

    def ldap_disable(self, name, fqdn, ldap_suffix):
        assert isinstance(ldap_suffix, DN)

        entry_dn = DN(('cn', name), ('cn', fqdn), ('cn', 'masters'),
                        ('cn', 'ipa'), ('cn', 'etc'), ldap_suffix)
        search_kw = {'ipaConfigString': u'enabledService'}
        filter = api.Backend.ldap2.make_filter(search_kw)
        try:
            entries, _truncated = api.Backend.ldap2.find_entries(
                filter=filter,
                attrs_list=['ipaConfigString'],
                base_dn=entry_dn,
                scope=api.Backend.ldap2.SCOPE_BASE)
        except errors.NotFound:
            root_logger.debug("service %s startup entry already disabled", name)
            return

        assert len(entries) == 1  # only one entry is expected
        entry = entries[0]

        # case insensitive
        for value in entry.get('ipaConfigString', []):
            if value.lower() == u'enabledservice':
                entry['ipaConfigString'].remove(value)
                break

        try:
            api.Backend.ldap2.update_entry(entry)
        except errors.EmptyModlist:
            pass
        except:
            root_logger.debug("failed to disable service %s startup entry", name)
            raise

        root_logger.debug("service %s startup entry disabled", name)

    def ldap_remove_service_container(self, name, fqdn, ldap_suffix):
        entry_dn = DN(('cn', name), ('cn', fqdn), ('cn', 'masters'),
                        ('cn', 'ipa'), ('cn', 'etc'), ldap_suffix)
        try:
            api.Backend.ldap2.delete_entry(entry_dn)
        except errors.NotFound:
            root_logger.debug("service %s container already removed", name)
        else:
            root_logger.debug("service %s container sucessfully removed", name)

    def _add_service_principal(self):
        try:
            self.api.Command.service_add(self.principal, force=True)
        except errors.DuplicateEntry:
            pass

    def _run_getkeytab(self):
        """
        backup and remove old service keytab (if present) and fetch a new one
        using ipa-getkeytab. This assumes that the service principal is already
        created in LDAP. By default GSSAPI authentication is used unless:
            * LDAPI socket is used and effective process UID is 0, then
              autobind is used by EXTERNAL SASL mech
            * self.dm_password is not none, then DM credentials are used to
              fetch keytab
        """
        self.fstore.backup_file(self.keytab)
        try:
            os.unlink(self.keytab)
        except OSError:
            pass

        ldap_uri = self.api.env.ldap_uri
        args = [paths.IPA_GETKEYTAB,
                '-k', self.keytab,
                '-p', self.principal,
                '-H', ldap_uri]
        nolog = tuple()

        if ldap_uri.startswith("ldapi://") and os.geteuid() == 0:
            args.extend(["-Y", "EXTERNAL"])
        elif self.dm_password is not None and not self.promote:
            args.extend(
                ['-D', 'cn=Directory Manager',
                 '-w', self.dm_password])
            nolog += (self.dm_password,)

        ipautil.run(args, nolog=nolog)

    def _request_service_keytab(self):
        if any(attr is None for attr in (self.principal, self.keytab,
                                         self.service_user)):
            raise NotImplementedError(
                "service must have defined principal "
                "name, keytab, and username")

        self._add_service_principal()
        self._run_getkeytab()

        pent = pwd.getpwnam(self.service_user)
        os.chown(self.keytab, pent.pw_uid, pent.pw_gid)


class SimpleServiceInstance(Service):
    def create_instance(self, gensvc_name=None, fqdn=None, ldap_suffix=None,
                        realm=None):
        self.gensvc_name = gensvc_name
        self.fqdn = fqdn
        self.suffix = ldap_suffix
        self.realm = realm

        self.step("starting %s " % self.service_name, self.__start)
        self.step("configuring %s to start on boot" % self.service_name, self.__enable)
        self.start_creation("Configuring %s" % self.service_name)

    suffix = ipautil.dn_attribute_property('_ldap_suffix')

    def __start(self):
        self.backup_state("running", self.is_running())
        self.restart()

    def __enable(self):
        self.backup_state("enabled", self.is_enabled())
        if self.gensvc_name == None:
            self.enable()
        else:
            self.ldap_enable(self.gensvc_name, self.fqdn, None, self.suffix)

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring %s" % self.service_name)

        self.stop()
        self.disable()

        running = self.restore_state("running")
        enabled = self.restore_state("enabled")

        # restore the original state of service
        if running:
            self.start()
        if enabled:
            self.enable()
