# Copyright 2014 Cloudbase Solutions SRL.
# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.boot.windows`."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

import httplib
import json
import os
import shutil
import urllib2

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
import mock
from provisioningserver.boot import (
    BootMethodError,
    BytesReader,
    windows as windows_module,
    )
from provisioningserver.boot.windows import (
    Bcd,
    WindowsPXEBootMethod,
    )
from provisioningserver.config import Config
from provisioningserver.tests.test_kernel_opts import make_kernel_parameters
from testtools.deferredruntest import AsynchronousDeferredRunTest
from tftp.backend import FilesystemReader
from twisted.internet.defer import inlineCallbacks
from twisted.python import context


class TestBcd(MAASTestCase):

    def configure_hivex(self):
        mock_hivex = mock.MagicMock()
        self.patch(windows_module, 'load_hivex').return_value = mock_hivex
        mock_hivex.node_name.side_effect = ['Objects',
                                            Bcd.GUID_WINDOWS_BOOTMGR,
                                            Bcd.BOOT_MGR_DISPLAY_ORDER]
        mock_hivex.node_children.side_effect = [
            [factory.make_name('objects')], [factory.make_name('object')],
            ['value0', factory.make_UUID()],
            [factory.make_name('element')]]
        mock_hivex.node_values.return_value = [factory.make_name('val')]

    def configure_bcd(self, uids=None):
        self.configure_hivex()
        filename = factory.make_name('filename')
        bcd = Bcd(filename)
        bcd.uids = mock.MagicMock(spec=dict)
        if uids is None:
            uids = [factory.make_name('uid'),
                    factory.make_name('uid')]
        bcd.uids.__getitem__.return_value = uids
        bcd.hive = mock.MagicMock()
        return bcd

    def test_get_loader(self):
        bcd = self.configure_bcd()

        mock_elem = factory.make_name('elem')
        bootmgr_elems = mock.MagicMock(spec=dict)
        bootmgr_elems.__getitem__.return_value = mock_elem

        mock_node_value = factory.make_name('node_value')
        bcd.hive.node_values.return_value = [mock_node_value]
        mock_string = factory.make_name('strings')
        bcd.hive.value_multiple_strings.return_value = [mock_string]

        response = bcd._get_loader(bootmgr_elems)
        self.assertThat(bcd.hive.node_values, MockCalledOnceWith(mock_elem))
        self.assertThat(
            bcd.hive.value_multiple_strings,
            MockCalledOnceWith(mock_node_value))
        self.assertEqual(mock_string, response)

    def test_get_loader_elems(self):
        mock_uid_0 = factory.make_name('uid')
        mock_uid_1 = factory.make_name('uid')
        bcd = self.configure_bcd(uids=[mock_uid_0, mock_uid_1])

        mock_child = factory.make_name('child')
        bcd.hive.node_children.side_effect = [[mock_child]]
        mock_name = factory.make_name('name')
        bcd.hive.node_name.return_value = mock_name

        response = bcd._get_loader_elems()
        self.assertThat(bcd.hive.node_children, MockCalledOnceWith(mock_uid_1))
        self.assertThat(bcd.hive.node_name, MockCalledOnceWith(mock_child))
        self.assertEqual(response, {mock_name: mock_child})

    def test_get_load_options_key(self):
        bcd = self.configure_bcd()

        fake_load_elem = factory.make_name('load_elem')
        mock_load_elem = mock.MagicMock()
        mock_load_elem.get.return_value = fake_load_elem

        mock_get_loader_elems = self.patch(Bcd, '_get_loader_elems')
        mock_get_loader_elems.return_value = mock_load_elem

        response = bcd._get_load_options_key()
        self.assertThat(
            mock_get_loader_elems, MockCalledOnceWith())
        self.assertThat(
            mock_load_elem.get, MockCalledOnceWith(bcd.LOAD_OPTIONS, None))
        self.assertEqual(response, fake_load_elem)

    def test_set_load_options(self):
        mock_uid_0 = factory.make_name('uid')
        mock_uid_1 = factory.make_name('uid')
        bcd = self.configure_bcd(uids=[mock_uid_0, mock_uid_1])

        fake_value = factory.make_name('value')
        mock_get_load_options_key = self.patch(Bcd, '_get_load_options_key')
        mock_get_load_options_key.return_value = None

        fake_child = factory.make_name('child')
        bcd.hive.node_add_child.return_value = fake_child
        bcd.set_load_options(value=fake_value)

        compare = {'t': 1,
                   'key': "Element",
                   'value': fake_value.decode('utf-8').encode('utf-16le'),
                   }
        self.assertThat(
            mock_get_load_options_key, MockCalledOnceWith())
        self.assertThat(
            bcd.hive.node_add_child,
            MockCalledOnceWith(mock_uid_1, bcd.LOAD_OPTIONS))
        self.assertThat(
            bcd.hive.node_set_value,
            MockCalledOnceWith(fake_child, compare))
        self.assertThat(bcd.hive.commit, MockCalledOnceWith(None))


class TestWindowsPXEBootMethod(MAASTestCase):

    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=5)

    def setUp(self):
        self.patch(Config, 'load_from_cache')
        self.patch(windows_module, 'get_hivex_module')
        super(TestWindowsPXEBootMethod, self).setUp()

    def test_get_page_sync(self):
        data = [factory.make_name('response') for _ in range(3)]
        mock_urlopen = self.patch(urllib2, 'urlopen')
        mock_urlopen.return_value.getcode.return_value = httplib.OK
        mock_urlopen.return_value.read.return_value = json.dumps(data)
        method = WindowsPXEBootMethod()
        expected = method.get_page_sync(None)
        self.assertEqual(data, expected)

    def test_get_page_sync_not_OK(self):
        mock_urlopen = self.patch(urllib2, 'urlopen')
        mock_urlopen.return_value.getcode.return_value = httplib.NOT_FOUND
        method = WindowsPXEBootMethod()
        expected = method.get_page_sync(None)
        self.assertEqual(None, expected)

    def test_clean_path(self):
        method = WindowsPXEBootMethod()
        parts = [factory.make_string() for _ in range(3)]
        dirty_path = '\\'.join(parts)
        valid_path = dirty_path.lower().replace('\\', '/')
        clean_path = method.clean_path(dirty_path)
        self.assertEqual(valid_path, clean_path)

    def test_clean_path_strip_boot(self):
        method = WindowsPXEBootMethod()
        dirty_path = '\\Boot\\BCD'
        clean_path = method.clean_path(dirty_path)
        self.assertEqual('bcd', clean_path)

    @inlineCallbacks
    def test_get_node_info(self):
        method = WindowsPXEBootMethod()
        mock_mac = factory.getRandomMACAddress()
        mock_purpose = factory.make_name('install')
        mock_release = factory.make_name('release')
        mock_get_page = self.patch(method, 'get_page_sync')
        mock_get_page.return_value = {
            'purpose': mock_purpose,
            'release': mock_release,
            }
        self.patch(windows_module, 'get_remote_mac').return_value = mock_mac

        cluster_uuid = factory.make_UUID()
        self.patch(windows_module, 'get_cluster_uuid').return_value = (
            cluster_uuid)

        mock_backend = mock.MagicMock()
        mock_backend.get_cluster_uuid.return_value = factory.make_name('uuid')
        mock_backend.get_generator_url.return_value = factory.make_name('url')

        call_context = {
            "local": (
                factory.getRandomIPAddress(),
                factory.pick_port()),
            "remote": (
                factory.getRandomIPAddress(),
                factory.pick_port()),
            }

        data = yield context.call(
            call_context, method.get_node_info, mock_backend)

        self.assertThat(
            mock_get_page,
            MockCalledOnceWith(mock_backend.get_generator_url.return_value))
        self.assertEqual(mock_purpose, data['purpose'])
        self.assertEqual(mock_release, data['release'])
        self.assertEqual(mock_mac, data['mac'])

    def test_match_path_pxelinux(self):
        method = WindowsPXEBootMethod()
        method.remote_path = factory.make_string()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(method, 'get_node_info')
        mock_get_node_info.return_value = {
            'purpose': 'install',
            'osystem': 'windows',
            'mac': mock_mac,
            }

        params = method.match_path(None, 'pxelinux.0')
        self.assertEqual(mock_mac, params['mac'])
        self.assertEqual(method.bootloader_path, params['path'])

    def test_match_path_pxelinux_only_on_install(self):
        method = WindowsPXEBootMethod()
        method.remote_path = factory.make_string()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(method, 'get_node_info')
        mock_get_node_info.return_value = {
            'purpose': factory.make_string(),
            'osystem': 'windows',
            'mac': mock_mac,
            }

        params = method.match_path(None, 'pxelinux.0')
        self.assertEqual(params, None)

    def test_match_path_pxelinux_missing_hivex(self):
        method = WindowsPXEBootMethod()
        method.remote_path = factory.make_string()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(method, 'get_node_info')
        mock_get_node_info.return_value = {
            'purpose': factory.make_string(),
            'osystem': 'windows',
            'mac': mock_mac,
            }

        self.patch(windows_module, 'HAVE_HIVEX', )
        params = method.match_path(None, 'pxelinux.0')
        self.assertEqual(params, None)

    def test_match_path_pxelinux_only_on_windows(self):
        method = WindowsPXEBootMethod()
        method.remote_path = factory.make_string()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(method, 'get_node_info')
        mock_get_node_info.return_value = {
            'purpose': 'install',
            'osystem': factory.make_string(),
            'mac': mock_mac,
            }

        params = method.match_path(None, 'pxelinux.0')
        self.assertEqual(params, None)

    def test_match_path_pxelinux_get_node_info_None(self):
        method = WindowsPXEBootMethod()
        method.remote_path = factory.make_string()
        mock_get_node_info = self.patch(method, 'get_node_info')
        mock_get_node_info.return_value = None

        params = method.match_path(None, 'pxelinux.0')
        self.assertEqual(params, None)

    def test_match_path_static_file(self):
        method = WindowsPXEBootMethod()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(windows_module, 'get_remote_mac')
        mock_get_node_info.return_value = mock_mac

        params = method.match_path(None, 'bootmgr.exe')
        self.assertEqual(mock_mac, params['mac'])
        self.assertEqual('bootmgr.exe', params['path'])

    def test_match_path_static_file_clean_path(self):
        method = WindowsPXEBootMethod()
        mock_mac = factory.getRandomMACAddress()
        mock_get_node_info = self.patch(windows_module, 'get_remote_mac')
        mock_get_node_info.return_value = mock_mac

        params = method.match_path(None, '\\Boot\\BCD')
        self.assertEqual(mock_mac, params['mac'])
        self.assertEqual('bcd', params['path'])

    def test_get_reader_bcd(self):
        method = WindowsPXEBootMethod()
        mock_compose_bcd = self.patch(method, 'compose_bcd')
        local_host = factory.getRandomIPAddress()
        kernel_params = make_kernel_parameters(osystem='windows')

        method.get_reader(
            None, kernel_params, path='bcd', local_host=local_host)
        self.assertThat(
            mock_compose_bcd, MockCalledOnceWith(kernel_params, local_host))

    def test_get_reader_static_file(self):
        method = WindowsPXEBootMethod()
        mock_path = factory.make_name('path')
        mock_output_static = self.patch(method, 'output_static')
        kernel_params = make_kernel_parameters(osystem='windows')

        method.get_reader(None, kernel_params, path=mock_path)
        self.assertThat(
            mock_output_static,
            MockCalledOnceWith(kernel_params, mock_path))

    def test_compose_preseed_url(self):
        url = 'http://localhost/MAAS'
        expected = 'http:\\\\localhost\\^M^A^A^S'
        method = WindowsPXEBootMethod()
        output = method.compose_preseed_url(url)
        self.assertEqual(expected, output)

    def test_compose_bcd(self):
        method = WindowsPXEBootMethod()
        local_host = factory.getRandomIPAddress()
        kernel_params = make_kernel_parameters()

        fake_output = factory.make_string().encode('utf-8')
        self.patch(os.path, 'isfile').return_value = True
        self.patch(shutil, 'copyfile')
        self.patch(windows_module, 'Bcd')

        with mock.patch(
                'provisioningserver.boot.windows.open',
                mock.mock_open(read_data=fake_output), create=True):
            output = method.compose_bcd(kernel_params, local_host)

        self.assertTrue(isinstance(output, BytesReader))
        self.assertEqual(fake_output, output.read(-1))

    def test_compose_bcd_missing_template(self):
        method = WindowsPXEBootMethod()
        self.patch(method, 'get_resource_path').return_value = ''
        local_host = factory.getRandomIPAddress()
        kernel_params = make_kernel_parameters()

        self.assertRaises(
            BootMethodError, method.compose_bcd, kernel_params, local_host)

    def test_get_resouce_path(self):
        fake_tftproot = factory.make_name('tftproot')
        mock_config = self.patch(windows_module, 'Config')
        mock_config.load_from_cache.return_value = {
            'tftp': {
                'resource_root': fake_tftproot,
                },
            }
        method = WindowsPXEBootMethod()
        fake_path = factory.make_name('path')
        fake_kernelparams = make_kernel_parameters()
        result = method.get_resource_path(fake_kernelparams, fake_path)
        expected = os.path.join(
            fake_tftproot, 'windows', fake_kernelparams.arch,
            fake_kernelparams.subarch, fake_kernelparams.release,
            fake_kernelparams.label, fake_path)
        self.assertEqual(expected, result)

    def test_output_static(self):
        method = WindowsPXEBootMethod()
        contents = factory.make_string()
        temp_dir = self.make_dir()
        filename = factory.make_file(temp_dir, "resource", contents=contents)
        self.patch(method, 'get_resource_path').return_value = filename
        result = method.output_static(None, None)
        self.assertIsInstance(result, FilesystemReader)
        self.assertEqual(contents, result.read(10000))
