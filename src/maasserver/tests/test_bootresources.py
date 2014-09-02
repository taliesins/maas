# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver.bootresources."""

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
from os import environ
from random import randint
from StringIO import StringIO

from django.core.urlresolvers import reverse
from django.db import (
    connections,
    transaction,
    )
from django.http import StreamingHttpResponse
from django.test.client import Client
from maasserver import bootresources
from maasserver.bootresources import (
    BootResourceStore,
    download_all_boot_resources,
    download_boot_resources,
    ensure_boot_source_definition,
    get_simplestream_endpoint,
    )
from maasserver.enum import (
    BOOT_RESOURCE_FILE_TYPE,
    BOOT_RESOURCE_TYPE,
    )
from maasserver.models import (
    BootResource,
    BootResourceFile,
    BootResourceSet,
    BootSource,
    BootSourceSelection,
    LargeFile,
    )
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_object
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils import absolute_reverse
from maasserver.utils.orm import get_one
from maastesting.djangotestcase import TransactionTestCase
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
    )
from maastesting.testcase import MAASTestCase
from mock import (
    MagicMock,
    Mock,
    sentinel,
    )
from provisioningserver.import_images.product_mapping import ProductMapping
from testtools.matchers import (
    ContainsAll,
    HasLength,
    )


def make_boot_resource_file_with_stream():
    resource = factory.make_usable_boot_resource(
        rtype=BOOT_RESOURCE_TYPE.SYNCED)
    rfile = resource.sets.first().files.first()
    with rfile.largefile.content.open('rb') as stream:
        content = stream.read()
    with rfile.largefile.content.open('wb') as stream:
        stream.truncate()
    return rfile, StringIO(content), content


class TestHelpers(MAASServerTestCase):
    """Tests for `maasserver.bootresources` helpers."""

    def test_get_simplestreams_endpoint(self):
        endpoint = get_simplestream_endpoint()
        self.assertEqual(
            absolute_reverse(
                'simplestreams_stream_handler',
                kwargs={'filename': 'index.json'}),
            endpoint['url'])
        self.assertEqual([], endpoint['selections'])

    def test_ensure_boot_source_definition_creates_default_source(self):
        ensure_boot_source_definition()
        sources = BootSource.objects.all()
        self.assertThat(sources, HasLength(1))
        [source] = sources
        self.assertAttributes(
            source,
            {
                'url': 'http://maas.ubuntu.com/images/ephemeral-v2/releases/',
                'keyring_filename': (
                    '/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg'),
            })
        selections = BootSourceSelection.objects.filter(boot_source=source)
        by_release = {
            selection.release: selection
            for selection in selections
            }
        self.assertItemsEqual(['trusty'], by_release.keys())
        self.assertAttributes(
            by_release['trusty'],
            {
                'release': 'trusty',
                'arches': ['amd64'],
                'subarches': ['*'],
                'labels': ['release'],
            })

    def test_ensure_boot_source_definition_skips_if_already_present(self):
        sources = [
            factory.make_boot_source()
            for _ in range(3)
            ]
        ensure_boot_source_definition()
        self.assertItemsEqual(sources, BootSource.objects.all())


class TestSimpleStreamsHandler(MAASServerTestCase):
    """Tests for `maasserver.bootresources.SimpleStreamsHandler`."""

    def reverse_stream_handler(self, filename):
        return reverse(
            'simplestreams_stream_handler', kwargs={'filename': filename})

    def reverse_file_handler(
            self, os, arch, subarch, series, version, filename):
        return reverse(
            'simplestreams_file_handler', kwargs={
                'os': os,
                'arch': arch,
                'subarch': subarch,
                'series': series,
                'version': version,
                'filename': filename,
                })

    def get_stream_client(self, filename):
        return self.client.get(self.reverse_stream_handler(filename))

    def get_file_client(self, os, arch, subarch, series, version, filename):
        return self.client.get(
            self.reverse_file_handler(
                os, arch, subarch, series, version, filename))

    def get_product_name_for_resource(self, resource):
        arch, subarch = resource.architecture.split('/')
        if resource.rtype == BOOT_RESOURCE_TYPE.UPLOADED:
            os = 'custom'
            series = resource.name
        else:
            os, series = resource.name.split('/')
        return 'maas:boot:%s:%s:%s:%s' % (os, arch, subarch, series)

    def make_usable_product_boot_resource(self):
        resource = factory.make_usable_boot_resource()
        return self.get_product_name_for_resource(resource), resource

    def test_streams_other_than_allowed_returns_404(self):
        allowed_paths = [
            'index.json',
            'maas:v2:download.json',
            ]
        invalid_paths = [
            '%s.json' % factory.make_name('path')
            for _ in range(3)
            ]
        for path in allowed_paths:
            response = self.get_stream_client(path)
            self.assertEqual(httplib.OK, response.status_code)
        for path in invalid_paths:
            response = self.get_stream_client(path)
            self.assertEqual(httplib.NOT_FOUND, response.status_code)

    def test_streams_product_index_contains_keys(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertThat(output, ContainsAll(['index', 'updated', 'format']))

    def test_streams_product_index_format_is_index_1(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertEqual('index:1.0', output['format'])

    def test_streams_product_index_index_has_maas_v2_download(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertThat(output['index'], ContainsAll(['maas:v2:download']))

    def test_streams_product_index_maas_v2_download_contains_keys(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertThat(
            output['index']['maas:v2:download'],
            ContainsAll([
                'datatype', 'path', 'updated', 'products', 'format']))

    def test_streams_product_index_maas_v2_download_has_valid_values(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertEqual(
            'image-downloads',
            output['index']['maas:v2:download']['datatype'])
        self.assertEqual(
            'streams/v1/maas:v2:download.json',
            output['index']['maas:v2:download']['path'])
        self.assertEqual(
            'products:1.0',
            output['index']['maas:v2:download']['format'])

    def test_streams_product_index_empty_products(self):
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertEqual(
            [],
            output['index']['maas:v2:download']['products'])

    def test_streams_product_index_empty_with_incomplete_resource(self):
        resource = factory.make_boot_resource()
        factory.make_boot_resource_set(resource)
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        self.assertEqual(
            [],
            output['index']['maas:v2:download']['products'])

    def test_streams_product_index_with_resources(self):
        products = []
        for _ in range(3):
            product, _ = self.make_usable_product_boot_resource()
            products.append(product)
        response = self.get_stream_client('index.json')
        output = json.loads(response.content)
        # Product listing should be the same as all of the completed
        # boot resources in the database.
        self.assertItemsEqual(
            products,
            output['index']['maas:v2:download']['products'])

    def test_streams_product_download_contains_keys(self):
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertThat(output, ContainsAll([
            'datatype', 'updated', 'content_id', 'products', 'format']))

    def test_streams_product_download_has_valid_values(self):
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertEqual('image-downloads', output['datatype'])
        self.assertEqual('maas:v2:download', output['content_id'])
        self.assertEqual('products:1.0', output['format'])

    def test_streams_product_download_empty_products(self):
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertEqual(
            {},
            output['products'])

    def test_streams_product_download_empty_with_incomplete_resource(self):
        resource = factory.make_boot_resource()
        factory.make_boot_resource_set(resource)
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertEqual(
            {},
            output['products'])

    def test_streams_product_download_has_valid_product_keys(self):
        products = []
        for _ in range(3):
            product, _ = self.make_usable_product_boot_resource()
            products.append(product)
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        # Product listing should be the same as all of the completed
        # boot resources in the database.
        self.assertThat(
            output['products'],
            ContainsAll(products))

    def test_streams_product_download_product_contains_keys(self):
        product, _ = self.make_usable_product_boot_resource()
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertThat(
            output['products'][product],
            ContainsAll([
                'versions', 'subarch', 'label', 'version',
                'arch', 'release', 'krel', 'os']))

    def test_streams_product_download_product_has_valid_values(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        label = resource.get_latest_complete_set().label
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        output_product = output['products'][product]
        self.assertEqual(subarch, output_product['subarch'])
        self.assertEqual(label, output_product['label'])
        self.assertEqual(series, output_product['version'])
        self.assertEqual(arch, output_product['arch'])
        self.assertEqual(series, output_product['release'])
        self.assertEqual(series, output_product['krel'])
        self.assertEqual(os, output_product['os'])
        for key, value in resource.extra.items():
            self.assertEqual(value, output_product[key])

    def test_streams_product_download_product_uses_latest_complete_label(self):
        product, resource = self.make_usable_product_boot_resource()
        # Incomplete resource_set
        factory.make_boot_resource_set(resource)
        newest_set = factory.make_boot_resource_set(resource)
        factory.make_boot_resource_file_with_content(newest_set)
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        output_product = output['products'][product]
        self.assertEqual(newest_set.label, output_product['label'])

    def test_streams_product_download_product_contains_multiple_versions(self):
        resource = factory.make_boot_resource()
        resource_sets = [
            factory.make_boot_resource_set(resource)
            for _ in range(3)
            ]
        versions = []
        for resource_set in resource_sets:
            factory.make_boot_resource_file_with_content(resource_set)
            versions.append(resource_set.version)
        product = self.get_product_name_for_resource(resource)
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        self.assertThat(
            output['products'][product]['versions'],
            ContainsAll(versions))

    def test_streams_product_download_product_version_contains_items(self):
        product, resource = self.make_usable_product_boot_resource()
        resource_set = resource.get_latest_complete_set()
        items = [
            rfile.filename
            for rfile in resource_set.files.all()
            ]
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        version = output['products'][product]['versions'][resource_set.version]
        self.assertThat(
            version['items'],
            ContainsAll(items))

    def test_streams_product_download_product_item_contains_keys(self):
        product, resource = self.make_usable_product_boot_resource()
        resource_set = resource.get_latest_complete_set()
        resource_file = resource_set.files.order_by('?')[0]
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        version = output['products'][product]['versions'][resource_set.version]
        self.assertThat(
            version['items'][resource_file.filename],
            ContainsAll(['path', 'ftype', 'sha256', 'size']))

    def test_streams_product_download_product_item_has_valid_values(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        resource_set = resource.get_latest_complete_set()
        resource_file = resource_set.files.order_by('?')[0]
        path = '%s/%s/%s/%s/%s/%s' % (
            os, arch, subarch, series, resource_set.version,
            resource_file.filename)
        response = self.get_stream_client('maas:v2:download.json')
        output = json.loads(response.content)
        version = output['products'][product]['versions'][resource_set.version]
        item = version['items'][resource_file.filename]
        self.assertEqual(path, item['path'])
        self.assertEqual(resource_file.filetype, item['ftype'])
        self.assertEqual(resource_file.largefile.sha256, item['sha256'])
        self.assertEqual(resource_file.largefile.total_size, item['size'])
        for key, value in resource_file.extra.items():
            self.assertEqual(value, item[key])

    def test_download_invalid_boot_resource_returns_404(self):
        os = factory.make_name('os')
        series = factory.make_name('series')
        arch = factory.make_name('arch')
        subarch = factory.make_name('subarch')
        version = factory.make_name('version')
        filename = factory.make_name('filename')
        response = self.get_file_client(
            os, arch, subarch, series, version, filename)
        self.assertEqual(httplib.NOT_FOUND, response.status_code)

    def test_download_invalid_version_returns_404(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        version = factory.make_name('version')
        filename = factory.make_name('filename')
        response = self.get_file_client(
            os, arch, subarch, series, version, filename)
        self.assertEqual(httplib.NOT_FOUND, response.status_code)

    def test_download_invalid_filename_returns_404(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        filename = factory.make_name('filename')
        response = self.get_file_client(
            os, arch, subarch, series, version, filename)
        self.assertEqual(httplib.NOT_FOUND, response.status_code)

    def test_download_valid_path_returns_200(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        resource_file = resource_set.files.order_by('?')[0]
        filename = resource_file.filename
        response = self.get_file_client(
            os, arch, subarch, series, version, filename)
        self.assertEqual(httplib.OK, response.status_code)

    def test_download_returns_streaming_response(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(':')
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        resource_file = resource_set.files.order_by('?')[0]
        filename = resource_file.filename
        response = self.get_file_client(
            os, arch, subarch, series, version, filename)
        self.assertIsInstance(response, StreamingHttpResponse)


class TestConnectionWrapper(TransactionTestCase):
    """Tests the use of StreamingHttpResponse(ConnectionWrapper(stream)).

    We do not run this inside of `MAASServerTestCase` as that wraps a
    transaction around each test. Since a new connection is created to return
    the actual content, the transaction to create the data needs be committed.
    """

    def make_file_for_client(self):
        # Set up the database information inside of a transaction. This is
        # done so the information is committed. As the new connection needs
        # to be able to access the data.
        with transaction.atomic():
            os = factory.make_name('os')
            series = factory.make_name('series')
            arch = factory.make_name('arch')
            subarch = factory.make_name('subarch')
            name = '%s/%s' % (os, series)
            architecture = '%s/%s' % (arch, subarch)
            version = factory.make_name('version')
            filetype = factory.pick_enum(BOOT_RESOURCE_FILE_TYPE)
            # We set the filename to the same value as filetype, as in most
            # cases this will always be true. The simplestreams content from
            # maas.ubuntu.com, is formatted this way.
            filename = filetype
            size = randint(1024, 2048)
            content = factory.make_string(size=size)
            resource = factory.make_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
                architecture=architecture)
            resource_set = factory.make_boot_resource_set(
                resource, version=version)
            largefile = factory.make_large_file(content=content, size=size)
            factory.make_boot_resource_file(
                resource_set, largefile, filename=filename, filetype=filetype)
        return content, reverse(
            'simplestreams_file_handler', kwargs={
                'os': os,
                'arch': arch,
                'subarch': subarch,
                'series': series,
                'version': version,
                'filename': filename,
                })

    def read_response(self, response):
        """Read the streaming_content from the response.

        :rtype: bytes
        """
        return b''.join(response.streaming_content)

    def test_download_calls__get_new_connection(self):
        content, url = self.make_file_for_client()
        mock_get_new_connection = self.patch(
            bootresources.ConnectionWrapper, '_get_new_connection')

        client = Client()
        response = client.get(url)
        self.read_response(response)
        self.assertThat(mock_get_new_connection, MockCalledOnceWith())

    def test_download_connection_is_not_same_as_django_connections(self):
        content, url = self.make_file_for_client()

        class AssertConnectionWrapper(bootresources.ConnectionWrapper):

            def _set_up(self):
                super(AssertConnectionWrapper, self)._set_up()
                # Capture the created connection
                AssertConnectionWrapper.connection = self._connection

            def close(self):
                # Close the stream, but we don't want to close the
                # connection as the test is testing that the connection is
                # not the same as the connection django is using for other
                # webrequests.
                if self._stream is not None:
                    self._stream.close()
                    self._stream = None
                self._connection = None

        self.patch(
            bootresources, 'ConnectionWrapper', AssertConnectionWrapper)

        client = Client()
        response = client.get(url)
        self.read_response(response)

        # Add cleanup to close the connection, since this was removed from
        # AssertConnectionWrapper.close method.
        def close():
            conn = AssertConnectionWrapper.connection
            conn.commit()
            conn.leave_transaction_management()
            conn.close()
        self.addCleanup(close)

        # The connection that is used by the wrapper cannot be the same as the
        # connection be using for all other webrequests. Without this
        # seperate the transactional middleware will fail to initialize,
        # because the the connection will already be in a transaction.
        #
        # Note: cannot test if DatabaseWrapper != DatabaseWrapper, as it will
        # report true, because the __eq__ operator only checks if the aliases
        # are the same. This is checking the underlying connection is
        # different, which is the important part.
        self.assertNotEqual(
            connections["default"].connection,
            AssertConnectionWrapper.connection.connection)


def make_product():
    """Make product dictionary that is just like the one provided
    from simplsetreams."""
    subarch = factory.make_name('subarch')
    subarches = [factory.make_name('subarch') for _ in range(3)]
    subarches.insert(0, subarch)
    subarches = ','.join(subarches)
    product = {
        'os': factory.make_name('os'),
        'arch': factory.make_name('arch'),
        'subarch': subarch,
        'release': factory.make_name('release'),
        'kflavor': factory.make_name('kflavor'),
        'subarches': subarches,
        'version_name': factory.make_name('version'),
        'label': factory.make_name('label'),
        'ftype': factory.pick_enum(BOOT_RESOURCE_FILE_TYPE),
        'kpackage': factory.make_name('kpackage'),
        'di_version': factory.make_name('di_version'),
        }
    name = '%s/%s' % (product['os'], product['release'])
    architecture = '%s/%s' % (product['arch'], product['subarch'])
    return name, architecture, product


def make_boot_resource_group(
        rtype=None, name=None, architecture=None,
        version=None, filename=None, filetype=None):
    """Make boot resource that contains one set and one file."""
    resource = factory.make_boot_resource(
        rtype=rtype, name=name, architecture=architecture)
    resource_set = factory.make_boot_resource_set(resource, version=version)
    rfile = factory.make_boot_resource_file_with_content(
        resource_set, filename=filename, filetype=filetype)
    return resource, resource_set, rfile


def make_boot_resource_group_from_product(product):
    """Make boot resource that contains one set and one file, using the
    information from the given product.

    The product dictionary is also updated to include the sha256 and size
    for the created largefile. The calling function should use the returned
    product in place of the passed product.
    """
    name = '%s/%s' % (product['os'], product['release'])
    architecture = '%s/%s' % (product['arch'], product['subarch'])
    resource = factory.make_boot_resource(
        rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
        architecture=architecture)
    resource_set = factory.make_boot_resource_set(
        resource, version=product['version_name'])
    rfile = factory.make_boot_resource_file_with_content(
        resource_set, filename=product['ftype'],
        filetype=product['ftype'])
    product['sha256'] = rfile.largefile.sha256
    product['size'] = rfile.largefile.total_size
    return product, resource


class TestBootResourceStore(MAASServerTestCase):

    def make_boot_resources(self):
        resources = [
            factory.make_boot_resource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
            for _ in range(3)
            ]
        resource_names = []
        for resource in resources:
            os, series = resource.name.split('/')
            arch, subarch = resource.split_arch()
            name = '%s/%s/%s/%s' % (os, arch, subarch, series)
            resource_names.append(name)
        return resources, resource_names

    def test_init_initializes_variables(self):
        _, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        self.assertItemsEqual(resource_names, store._resources_to_delete)
        self.assertEqual({}, store._content_to_finalize)

    def test_prevent_resource_deletion_removes_resource(self):
        resources, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        resource = resources.pop()
        resource_names.pop()
        store.prevent_resource_deletion(resource)
        self.assertItemsEqual(resource_names, store._resources_to_delete)

    def test_prevent_resource_deletion_doesnt_remove_unknown_resource(self):
        resources, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        resource = factory.make_boot_resource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store.prevent_resource_deletion(resource)
        self.assertItemsEqual(resource_names, store._resources_to_delete)

    def test_save_content_later_adds_to__content_to_finalize_var(self):
        _, _, rfile = make_boot_resource_group()
        store = BootResourceStore()
        store.save_content_later(rfile, sentinel.reader)
        self.assertEqual(
            {rfile.id: sentinel.reader},
            store._content_to_finalize)

    def test_get_or_create_boot_resource_creates_resource(self):
        name, architecture, product = make_product()
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(BOOT_RESOURCE_TYPE.SYNCED, resource.rtype)
        self.assertEqual(name, resource.name)
        self.assertEqual(architecture, resource.architecture)
        self.assertEqual(product['kflavor'], resource.extra['kflavor'])
        self.assertEqual(product['subarches'], resource.extra['subarches'])

    def test_get_or_create_boot_resource_gets_resource(self):
        name, architecture, product = make_product()
        expected = factory.make_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
            architecture=architecture)
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(expected, resource)
        self.assertEqual(product['kflavor'], resource.extra['kflavor'])
        self.assertEqual(product['subarches'], resource.extra['subarches'])

    def test_get_or_create_boot_resource_calls_prevent_resource_deletion(self):
        name, architecture, product = make_product()
        resource = factory.make_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED,
            name=name, architecture=architecture)
        store = BootResourceStore()
        mock_prevent = self.patch(store, 'prevent_resource_deletion')
        store.get_or_create_boot_resource(product)
        self.assertThat(
            mock_prevent, MockCalledOnceWith(resource))

    def test_get_or_create_boot_resource_converts_generated_into_synced(self):
        name, architecture, product = make_product()
        resource = factory.make_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.GENERATED,
            name=name, architecture=architecture)
        store = BootResourceStore()
        mock_prevent = self.patch(store, 'prevent_resource_deletion')
        store.get_or_create_boot_resource(product)
        self.assertEqual(
            BOOT_RESOURCE_TYPE.SYNCED,
            reload_object(resource).rtype)
        self.assertThat(
            mock_prevent, MockNotCalled())

    def test_get_or_create_boot_resource_set_creates_resource_set(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource.sets.all().delete()
        store = BootResourceStore()
        resource_set = store.get_or_create_boot_resource_set(resource, product)
        self.assertEqual(product['version_name'], resource_set.version)
        self.assertEqual(product['label'], resource_set.label)

    def test_get_or_create_boot_resource_set_gets_resource_set(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        expected = resource.sets.first()
        store = BootResourceStore()
        resource_set = store.get_or_create_boot_resource_set(resource, product)
        self.assertEqual(expected, resource_set)
        self.assertEqual(product['label'], resource_set.label)

    def test_get_or_create_boot_resource_file_creates_resource_file(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        resource_set.files.all().delete()
        store = BootResourceStore()
        rfile = store.get_or_create_boot_resource_file(resource_set, product)
        self.assertEqual(product['ftype'], rfile.filename)
        self.assertEqual(product['ftype'], rfile.filetype)
        self.assertEqual(product['kpackage'], rfile.extra['kpackage'])
        self.assertEqual(product['di_version'], rfile.extra['di_version'])

    def test_get_or_create_boot_resource_file_gets_resource_file(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        expected = resource_set.files.first()
        store = BootResourceStore()
        rfile = store.get_or_create_boot_resource_file(resource_set, product)
        self.assertEqual(expected, rfile)
        self.assertEqual(product['ftype'], rfile.filetype)
        self.assertEqual(product['kpackage'], rfile.extra['kpackage'])
        self.assertEqual(product['di_version'], rfile.extra['di_version'])

    def test_get_resource_file_log_identifier_returns_valid_ident(self):
        os = factory.make_name('os')
        series = factory.make_name('series')
        arch = factory.make_name('arch')
        subarch = factory.make_name('subarch')
        version = factory.make_name('version')
        filename = factory.make_name('filename')
        name = '%s/%s' % (os, series)
        architecture = '%s/%s' % (arch, subarch)
        resource = factory.make_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
            architecture=architecture)
        resource_set = factory.make_boot_resource_set(
            resource, version=version)
        rfile = factory.make_boot_resource_file_with_content(
            resource_set, filename=filename)
        store = BootResourceStore()
        self.assertEqual(
            '%s/%s/%s/%s/%s/%s' % (
                os, arch, subarch, series, version, filename),
            store.get_resource_file_log_identifier(rfile))
        self.assertEqual(
            '%s/%s/%s/%s/%s/%s' % (
                os, arch, subarch, series, version, filename),
            store.get_resource_file_log_identifier(
                rfile, resource_set, resource))

    def test_write_content_saves_data(self):
        rfile, reader, content = make_boot_resource_file_with_stream()
        store = BootResourceStore()
        store.write_content(rfile, reader)
        self.assertTrue(BootResourceFile.objects.filter(id=rfile.id).exists())
        with rfile.largefile.content.open('rb') as stream:
            written_data = stream.read()
        self.assertEqual(content, written_data)

    def test_write_content_deletes_file_on_bad_checksum(self):
        rfile, _, _ = make_boot_resource_file_with_stream()
        reader = StringIO(factory.make_string())
        store = BootResourceStore()
        store.write_content(rfile, reader)
        self.assertFalse(BootResourceFile.objects.filter(id=rfile.id).exists())

    def test_finalize_calls_methods(self):
        store = BootResourceStore()
        mock_resource_cleaner = self.patch(store, 'resource_cleaner')
        mock_perform_write = self.patch(store, 'perform_write')
        mock_resource_set_cleaner = self.patch(store, 'resource_set_cleaner')
        store.finalize()
        self.assertTrue(mock_resource_cleaner, MockCalledOnceWith())
        self.assertTrue(mock_perform_write, MockCalledOnceWith())
        self.assertTrue(mock_resource_set_cleaner, MockCalledOnceWith())


class TestBootResourceTransactional(TransactionTestCase):
    """Test methods on `BootResourceStore` that manage their own transactions.

    This is done using TransactionTestCase so the database is flushed after
    each test run.
    """

    def test_insert_does_nothing_if_file_already_exists(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            rfile = resource.sets.first().files.first()
        largefile = rfile.largefile
        store = BootResourceStore()
        mock_save_later = self.patch(store, 'save_content_later')
        store.insert(product, sentinel.reader)
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_uses_already_existing_largefile(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            resource_set = resource.sets.first()
            resource_set.files.all().delete()
            largefile = factory.make_large_file()
        product['sha256'] = largefile.sha256
        product['size'] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, 'save_content_later')
        store.insert(product, sentinel.reader)
        self.assertEqual(
            largefile,
            get_one(reload_object(resource_set).files.all()).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_deletes_mismatch_largefile(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            rfile = resource.sets.first().files.first()
            delete_largefile = rfile.largefile
            largefile = factory.make_large_file()
        product['sha256'] = largefile.sha256
        product['size'] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, 'save_content_later')
        store.insert(product, sentinel.reader)
        self.assertFalse(
            LargeFile.objects.filter(id=delete_largefile.id).exists())
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_deletes_mismatch_largefile_keeps_other_resource_file(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            resource = factory.make_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
                architecture=architecture)
            resource_set = factory.make_boot_resource_set(
                resource, version=product['version_name'])
            other_type = factory.pick_enum(
                BOOT_RESOURCE_FILE_TYPE, but_not=product['ftype'])
            other_file = factory.make_boot_resource_file_with_content(
                resource_set, filename=other_type, filetype=other_type)
            rfile = factory.make_boot_resource_file(
                resource_set, other_file.largefile,
                filename=product['ftype'], filetype=product['ftype'])
            largefile = factory.make_large_file()
        product['sha256'] = largefile.sha256
        product['size'] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, 'save_content_later')
        store.insert(product, sentinel.reader)
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertTrue(
            LargeFile.objects.filter(id=other_file.largefile.id).exists())
        self.assertTrue(
            BootResourceFile.objects.filter(id=other_file.id).exists())
        self.assertEqual(
            other_file.largefile, reload_object(other_file).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_creates_new_largefile(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            resource = factory.make_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name,
                architecture=architecture)
            resource_set = factory.make_boot_resource_set(
                resource, version=product['version_name'])
        product['sha256'] = factory.make_string(size=64)
        product['size'] = randint(1024, 2048)
        store = BootResourceStore()
        mock_save_later = self.patch(store, 'save_content_later')
        store.insert(product, sentinel.reader)
        rfile = get_one(reload_object(resource_set).files.all())
        self.assertEqual(product['sha256'], rfile.largefile.sha256)
        self.assertEqual(product['size'], rfile.largefile.total_size)
        self.assertThat(
            mock_save_later,
            MockCalledOnceWith(rfile, sentinel.reader))

    def test_resource_cleaner_removes_old_boot_resources(self):
        with transaction.atomic():
            resources = [
                factory.make_boot_resource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
                for _ in range(3)
                ]
        store = BootResourceStore()
        store.resource_cleaner()
        for resource in resources:
            os, series = resource.name.split('/')
            arch, subarch = resource.split_arch()
            self.assertFalse(
                BootResource.objects.has_synced_resource(
                    os, arch, subarch, series))

    def test_resource_set_cleaner_removes_incomplete_set(self):
        with transaction.atomic():
            resource = factory.make_usable_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED)
            incomplete_set = factory.make_boot_resource_set(resource)
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertFalse(
            BootResourceSet.objects.filter(id=incomplete_set.id).exists())

    def test_resource_set_cleaner_keeps_only_newest_completed_set(self):
        with transaction.atomic():
            resource = factory.make_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED)
            old_complete_sets = []
            for _ in range(3):
                resource_set = factory.make_boot_resource_set(resource)
                factory.make_boot_resource_file_with_content(resource_set)
                old_complete_sets.append(resource_set)
            newest_set = factory.make_boot_resource_set(resource)
            factory.make_boot_resource_file_with_content(newest_set)
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertItemsEqual([newest_set], resource.sets.all())
        for resource_set in old_complete_sets:
            self.assertFalse(
                BootResourceSet.objects.filter(id=resource_set.id).exists())

    def test_resource_set_cleaner_removes_resources_with_empty_sets(self):
        with transaction.atomic():
            resource = factory.make_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertFalse(
            BootResource.objects.filter(id=resource.id).exists())

    def test_perform_writes_writes_all_content(self):
        with transaction.atomic():
            files = [make_boot_resource_file_with_stream() for _ in range(3)]
            store = BootResourceStore()
            for rfile, reader, content in files:
                store.save_content_later(rfile, reader)
        store.perform_write()
        with transaction.atomic():
            for rfile, reader, content in files:
                self.assertTrue(
                    BootResourceFile.objects.filter(id=rfile.id).exists())
                with rfile.largefile.content.open('rb') as stream:
                    written_data = stream.read()
                self.assertEqual(content, written_data)


class TestImportImages(MAASTestCase):

    def test_download_boot_resources_syncs_repo(self):
        fake_sync = self.patch(bootresources.BootResourceRepoWriter, 'sync')
        store = BootResourceStore()
        source_url = factory.make_url()
        download_boot_resources(
            source_url, store, None, None)
        self.assertEqual(1, len(fake_sync.mock_calls))

    def test_download_all_boot_resources_calls_download_boot_resources(self):
        source = {
            'url': factory.make_url(),
            'keyring': self.make_file("keyring"),
            }
        product_mapping = ProductMapping()
        store = BootResourceStore()
        fake_download = self.patch(bootresources, 'download_boot_resources')
        download_all_boot_resources(
            sources=[source], product_mapping=product_mapping, store=store)
        self.assertThat(
            fake_download,
            MockCalledOnceWith(
                source['url'], store, product_mapping,
                keyring_file=source['keyring']))

    def test_download_all_boot_resources_calls_finalize_on_store(self):
        product_mapping = ProductMapping()
        store = BootResourceStore()
        fake_finalize = self.patch(store, 'finalize')
        download_all_boot_resources(
            sources=[], product_mapping=product_mapping, store=store)
        self.assertThat(
            fake_finalize,
            MockCalledOnceWith())

    def test_has_synced_resources_returns_true(self):
        factory.make_boot_resource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        self.assertTrue(bootresources.has_synced_resources())

    def test_has_synced_resources_returns_false(self):
        factory.make_boot_resource(rtype=BOOT_RESOURCE_TYPE.UPLOADED)
        self.assertFalse(bootresources.has_synced_resources())

    def test__import_resources_exists_early_without_force(self):
        has_synced_resources = self.patch(
            bootresources, "has_synced_resources")
        bootresources._import_resources(force=False)
        # The test for already-synced resources is not performed if we're
        # forcing a sync.
        self.assertThat(has_synced_resources, MockCalledOnceWith())

    def test__import_resources_continues_with_force(self):
        has_synced_resources = self.patch(
            bootresources, "has_synced_resources")
        bootresources._import_resources(force=True)
        # The test for already-synced resources is performed if we're not
        # forcing a sync.
        self.assertThat(has_synced_resources, MockNotCalled())

    def test__import_resources_holds_lock(self):
        fake_write_all_keyrings = self.patch(
            bootresources, 'write_all_keyrings')

        def test_for_held_lock(sources):
            self.assertTrue(bootresources.locks.import_images.is_locked())
            return []
        fake_write_all_keyrings.side_effect = test_for_held_lock

        bootresources._import_resources(force=True)
        self.assertFalse(bootresources.locks.import_images.is_locked())

    def test__import_resources_calls_functions_with_correct_parameters(self):
        fake_write_all_keyrings = self.patch(
            bootresources, 'write_all_keyrings')
        fake_write_all_keyrings.return_value = sentinel.sources
        fake_image_descriptions = self.patch(
            bootresources, 'download_all_image_descriptions')
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        fake_image_descriptions.return_value = descriptions
        fake_map_products = self.patch(
            bootresources, 'map_products')
        fake_map_products.return_value = sentinel.mapping
        fake_download_all_boot_resources = self.patch(
            bootresources, 'download_all_boot_resources')

        bootresources._import_resources(force=True)

        self.assertThat(
            fake_write_all_keyrings,
            MockCalledOnceWith([]))
        self.assertThat(
            fake_image_descriptions,
            MockCalledOnceWith(sentinel.sources))
        self.assertThat(
            fake_map_products,
            MockCalledOnceWith(descriptions))
        self.assertThat(
            fake_download_all_boot_resources,
            MockCalledOnceWith(sentinel.sources, sentinel.mapping))

    def test__import_resources_has_env_GNUPGHOME_set(self):
        fake_image_descriptions = self.patch(
            bootresources, 'download_all_image_descriptions')
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        fake_image_descriptions.return_value = descriptions
        self.patch(bootresources, 'map_products')

        class CaptureEnv:
            """Fake function; records a copy of the environment."""

            def __call__(self, *args, **kwargs):
                self.args = args
                self.env = environ.copy()

        fake_download = self.patch(
            bootresources, 'download_all_boot_resources', CaptureEnv())

        bootresources._import_resources(force=True)
        self.assertEqual(
            bootresources.get_maas_user_gpghome(),
            fake_download.env['GNUPGHOME'])

    def test__import_resources_calls_import_boot_images_on_clusters(self):
        nodegroup = MagicMock()
        self.patch(bootresources, 'NodeGroup', nodegroup)

        fake_image_descriptions = self.patch(
            bootresources, 'download_all_image_descriptions')
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        fake_image_descriptions.return_value = descriptions
        self.patch(bootresources, 'map_products')
        self.patch(bootresources, 'download_all_boot_resources')

        bootresources._import_resources(force=True)
        self.assertThat(
            nodegroup.objects.import_boot_images_on_accepted_clusters,
            MockCalledOnceWith())
