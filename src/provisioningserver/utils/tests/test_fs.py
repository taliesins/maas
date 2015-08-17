# Copyright 2014-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for filesystem-related utilities."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
)

str = None

__metaclass__ = type
__all__ = []

from base64 import urlsafe_b64encode
import os
import os.path
from random import randint
import re
from shutil import rmtree
import stat
from subprocess import (
    CalledProcessError,
    PIPE,
)
import sys
import tempfile
import time

from maastesting import root
from maastesting.factory import factory
from maastesting.fakemethod import FakeMethod
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from mock import (
    ANY,
    call,
    Mock,
    sentinel,
)
import provisioningserver.config
from provisioningserver.utils.fs import (
    atomic_delete,
    atomic_symlink,
    atomic_write,
    ensure_dir,
    FileLock,
    get_maas_provision_command,
    get_mtime,
    incremental_write,
    pick_new_mtime,
    read_text_file,
    RunLock,
    sudo_delete_file,
    sudo_write_file,
    SystemLock,
    tempdir,
    write_text_file,
)
import provisioningserver.utils.fs as fs_module
from testtools.content import text_content
from testtools.matchers import (
    DirExists,
    EndsWith,
    Equals,
    FileContains,
    FileExists,
    IsInstance,
    MatchesAll,
    Not,
    SamePath,
    StartsWith,
)
from testtools.testcase import ExpectedException
from twisted.internet.task import Clock
from twisted.python import lockfile


class TestAtomicWrite(MAASTestCase):
    """Test `atomic_write`."""

    def test_atomic_write_overwrites_dest_file(self):
        content = factory.make_string()
        filename = self.make_file(contents=factory.make_string())
        atomic_write(content, filename)
        self.assertThat(filename, FileContains(content))

    def test_atomic_write_does_not_overwrite_file_if_overwrite_false(self):
        content = factory.make_string()
        random_content = factory.make_string()
        filename = self.make_file(contents=random_content)
        atomic_write(content, filename, overwrite=False)
        self.assertThat(filename, FileContains(random_content))

    def test_atomic_write_writes_file_if_no_file_present(self):
        filename = os.path.join(self.make_dir(), factory.make_string())
        content = factory.make_string()
        atomic_write(content, filename, overwrite=False)
        self.assertThat(filename, FileContains(content))

    def test_atomic_write_does_not_leak_temp_file_when_not_overwriting(self):
        # If the file is not written because it already exists and
        # overwriting was disabled, atomic_write does not leak its
        # temporary file.
        filename = self.make_file()
        atomic_write(factory.make_string(), filename, overwrite=False)
        self.assertEqual(
            [os.path.basename(filename)],
            os.listdir(os.path.dirname(filename)))

    def test_atomic_write_does_not_leak_temp_file_on_failure(self):
        # If the overwrite fails, atomic_write does not leak its
        # temporary file.
        self.patch(fs_module, 'rename', Mock(side_effect=OSError()))
        filename = self.make_file()
        with ExpectedException(OSError):
            atomic_write(factory.make_string(), filename)
        self.assertEqual(
            [os.path.basename(filename)],
            os.listdir(os.path.dirname(filename)))

    def test_atomic_write_sets_permissions(self):
        atomic_file = self.make_file()
        # Pick an unusual mode that is also likely to fall outside our
        # umask.  We want this mode set, not treated as advice that may
        # be tightened up by umask later.
        mode = 0o323
        atomic_write(factory.make_string(), atomic_file, mode=mode)
        self.assertEqual(mode, stat.S_IMODE(os.stat(atomic_file).st_mode))

    def test_atomic_write_sets_permissions_before_moving_into_place(self):

        recorded_modes = []

        def record_mode(source, dest):
            """Stub for os.rename: get source file's access mode."""
            recorded_modes.append(os.stat(source).st_mode)

        self.patch(fs_module, 'rename', Mock(side_effect=record_mode))
        playground = self.make_dir()
        atomic_file = os.path.join(playground, factory.make_name('atomic'))
        mode = 0o323
        atomic_write(factory.make_string(), atomic_file, mode=mode)
        [recorded_mode] = recorded_modes
        self.assertEqual(mode, stat.S_IMODE(recorded_mode))

    def test_atomic_write_preserves_ownership_before_moving_into_place(self):
        atomic_file = self.make_file('atomic')

        self.patch(fs_module, 'isfile').return_value = True
        self.patch(fs_module, 'chown')
        self.patch(fs_module, 'rename')
        self.patch(fs_module, 'stat')

        ret_stat = fs_module.stat.return_value
        ret_stat.st_uid = sentinel.uid
        ret_stat.st_gid = sentinel.gid
        ret_stat.st_mode = stat.S_IFREG

        atomic_write(factory.make_string(), atomic_file)

        self.assertThat(fs_module.stat, MockCalledOnceWith(atomic_file))
        self.assertThat(fs_module.chown, MockCalledOnceWith(
            ANY, sentinel.uid, sentinel.gid))

    def test_atomic_write_sets_OSError_filename_if_undefined(self):
        # When the filename attribute of an OSError is undefined when
        # attempting to create a temporary file, atomic_write fills it in with
        # a representative filename, similar to the specification required by
        # mktemp(1).
        mock_mkstemp = self.patch(tempfile, "mkstemp")
        mock_mkstemp.side_effect = OSError()
        filename = os.path.join("directory", "basename")
        error = self.assertRaises(OSError, atomic_write, "content", filename)
        self.assertEqual(
            os.path.join("directory", ".basename.XXXXXX.tmp"),
            error.filename)

    def test_atomic_write_does_not_set_OSError_filename_if_defined(self):
        # When the filename attribute of an OSError is defined when attempting
        # to create a temporary file, atomic_write leaves it alone.
        mock_mkstemp = self.patch(tempfile, "mkstemp")
        mock_mkstemp.side_effect = OSError()
        mock_mkstemp.side_effect.filename = factory.make_name("filename")
        filename = os.path.join("directory", "basename")
        error = self.assertRaises(OSError, atomic_write, "content", filename)
        self.assertEqual(
            mock_mkstemp.side_effect.filename,
            error.filename)


class TestAtomicDelete(MAASTestCase):
    """Test `atomic_delete`."""

    def test_atomic_delete_deletes_file(self):
        filename = self.make_file()
        atomic_delete(filename)
        self.assertThat(filename, Not(FileExists()))

    def test_renames_file_before_deleting(self):
        filename = self.make_file()
        del_filename = ".%s.del" % os.path.basename(filename)
        self.addCleanup(os.remove, del_filename)
        mock_remove = self.patch(fs_module.os, "remove")
        atomic_delete(filename)
        self.assertThat(del_filename, FileExists())
        self.assertThat(mock_remove, MockCalledOnceWith(del_filename))


class TestAtomicSymlink(MAASTestCase):
    """Test `atomic_symlink`."""

    def test_atomic_symlink_creates_symlink(self):
        filename = self.make_file(contents=factory.make_string())
        target_dir = self.make_dir()
        link_name = factory.make_name('link')
        target = os.path.join(target_dir, link_name)
        atomic_symlink(filename, target)
        self.assertTrue(
            os.path.islink(target), "atomic_symlink didn't create a symlink")
        self.assertThat(target, SamePath(filename))

    def test_atomic_symlink_overwrites_dest_file(self):
        filename = self.make_file(contents=factory.make_string())
        target_dir = self.make_dir()
        link_name = factory.make_name('link')
        # Create a file that will be overwritten.
        factory.make_file(location=target_dir, name=link_name)
        target = os.path.join(target_dir, link_name)
        atomic_symlink(filename, target)
        self.assertTrue(
            os.path.islink(target), "atomic_symlink didn't create a symlink")
        self.assertThat(target, SamePath(filename))

    def test_atomic_symlink_does_not_leak_temp_file_if_failure(self):
        # In the face of failure, no temp file is leaked.
        self.patch(os, 'rename', Mock(side_effect=OSError()))
        filename = self.make_file()
        target_dir = self.make_dir()
        link_name = factory.make_name('link')
        target = os.path.join(target_dir, link_name)
        with ExpectedException(OSError):
            atomic_symlink(filename, target)
        self.assertEqual(
            [],
            os.listdir(target_dir))


class TestIncrementalWrite(MAASTestCase):
    """Test `incremental_write`."""

    def test_incremental_write_increments_modification_time(self):
        content = factory.make_string()
        filename = self.make_file(contents=factory.make_string())
        # Pretend that this file is older than it is.  So that
        # incrementing its mtime won't put it in the future.
        old_mtime = os.stat(filename).st_mtime - 10
        os.utime(filename, (old_mtime, old_mtime))
        incremental_write(content, filename)
        self.assertAlmostEqual(
            os.stat(filename).st_mtime, old_mtime + 1, delta=0.01)

    def test_incremental_write_sets_permissions(self):
        atomic_file = self.make_file()
        mode = 0o323
        incremental_write(factory.make_string(), atomic_file, mode=mode)
        self.assertEqual(mode, stat.S_IMODE(os.stat(atomic_file).st_mode))


class TestGetMTime(MAASTestCase):
    """Test `get_mtime`."""

    def test_get_mtime_returns_None_for_nonexistent_file(self):
        nonexistent_file = os.path.join(
            self.make_dir(), factory.make_name('nonexistent-file'))
        self.assertIsNone(get_mtime(nonexistent_file))

    def test_get_mtime_returns_mtime(self):
        existing_file = self.make_file()
        mtime = os.stat(existing_file).st_mtime - randint(0, 100)
        os.utime(existing_file, (mtime, mtime))
        # Some small rounding/representation errors can happen here.
        # That's just the way of floating-point numbers.  According to
        # Gavin there's a conversion to fixed-point along the way, which
        # would raise representability issues.
        self.assertAlmostEqual(mtime, get_mtime(existing_file), delta=0.00001)

    def test_get_mtime_passes_on_other_error(self):
        forbidden_file = self.make_file()
        self.patch(os, 'stat', FakeMethod(failure=OSError("Forbidden file")))
        self.assertRaises(OSError, get_mtime, forbidden_file)


class TestPickNewMTime(MAASTestCase):
    """Test `pick_new_mtime`."""

    def test_pick_new_mtime_applies_starting_age_to_new_file(self):
        before = time.time()
        starting_age = randint(0, 5)
        recommended_age = pick_new_mtime(None, starting_age=starting_age)
        now = time.time()
        self.assertAlmostEqual(
            now - starting_age,
            recommended_age,
            delta=(now - before))

    def test_pick_new_mtime_increments_mtime_if_possible(self):
        past = time.time() - 2
        self.assertEqual(past + 1, pick_new_mtime(past))

    def test_pick_new_mtime_refuses_to_move_mtime_into_the_future(self):
        # Race condition: this will fail if the test gets held up for
        # a second between readings of the clock.
        now = time.time()
        self.assertEqual(now, pick_new_mtime(now))


class TestGetMAASProvisionCommand(MAASTestCase):

    def test__returns_just_command_for_production(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = False
        self.assertEqual("maas-provision", get_maas_provision_command())

    def test__returns_full_path_for_development(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = True
        self.assertEqual(
            root.rstrip("/") + "/bin/maas-provision",
            get_maas_provision_command())


class TestSudoWriteFile(MAASTestCase):
    """Testing for `sudo_write_file`."""

    def patch_popen(self, return_value=0):
        process = Mock()
        process.returncode = return_value
        process.communicate = Mock(return_value=('output', 'error output'))
        self.patch(fs_module, 'Popen', Mock(return_value=process))
        return process

    def test_calls_atomic_write(self):
        self.patch_popen()
        path = os.path.join(self.make_dir(), factory.make_name('file'))
        contents = factory.make_string()

        sudo_write_file(path, contents)

        self.assertThat(fs_module.Popen, MockCalledOnceWith(
            ['sudo', '-n', get_maas_provision_command(), 'atomic-write',
             '--filename', path, '--mode', '0644'], stdin=PIPE))

    def test_encodes_contents(self):
        process = self.patch_popen()
        contents = factory.make_string()
        encoding = 'utf-16'
        sudo_write_file(self.make_file(), contents, encoding=encoding)
        self.assertThat(
            process.communicate,
            MockCalledOnceWith(contents.encode(encoding)))

    def test_catches_failures(self):
        self.patch_popen(1)
        self.assertRaises(
            CalledProcessError,
            sudo_write_file, self.make_file(), factory.make_string())


class TestSudoDeleteFile(MAASTestCase):
    """Testing for `sudo_delete_file`."""

    def patch_popen(self, return_value=0):
        process = Mock()
        process.returncode = return_value
        process.communicate = Mock(return_value=('output', 'error output'))
        self.patch(fs_module, 'Popen', Mock(return_value=process))
        return process

    def test_calls_atomic_delete(self):
        self.patch_popen()
        path = os.path.join(self.make_dir(), factory.make_name('file'))

        sudo_delete_file(path)

        self.assertThat(fs_module.Popen, MockCalledOnceWith(
            ['sudo', '-n', get_maas_provision_command(), 'atomic-delete',
             '--filename', path]))

    def test_catches_failures(self):
        self.patch_popen(1)
        self.assertRaises(
            CalledProcessError,
            sudo_delete_file, self.make_file())


class TestEnsureDir(MAASTestCase):
    def test_succeeds_if_directory_already_existed(self):
        path = self.make_dir()
        ensure_dir(path)
        self.assertThat(path, DirExists())

    def test_fails_if_path_is_already_a_file(self):
        path = self.make_file()
        self.assertRaises(OSError, ensure_dir, path)
        self.assertThat(path, FileExists())

    def test_creates_dir_if_not_present(self):
        path = os.path.join(self.make_dir(), factory.make_name())
        ensure_dir(path)
        self.assertThat(path, DirExists())

    def test_passes_on_other_errors(self):
        not_a_dir = self.make_file()
        self.assertRaises(
            OSError,
            ensure_dir,
            os.path.join(not_a_dir, factory.make_name('impossible')))

    def test_creates_multiple_layers_of_directories_if_needed(self):
        path = os.path.join(
            self.make_dir(), factory.make_name('subdir'),
            factory.make_name('sbusubdir'))
        ensure_dir(path)
        self.assertThat(path, DirExists())


class TestTempDir(MAASTestCase):
    def test_creates_real_fresh_directory(self):
        stored_text = factory.make_string()
        filename = factory.make_name('test-file')
        with tempdir() as directory:
            self.assertThat(directory, DirExists())
            write_text_file(os.path.join(directory, filename), stored_text)
            retrieved_text = read_text_file(os.path.join(directory, filename))
            files = os.listdir(directory)

        self.assertEqual(stored_text, retrieved_text)
        self.assertEqual([filename], files)

    def test_creates_unique_directory(self):
        with tempdir() as dir1, tempdir() as dir2:
            pass
        self.assertNotEqual(dir1, dir2)

    def test_cleans_up_on_successful_exit(self):
        with tempdir() as directory:
            file_path = factory.make_file(directory)

        self.assertThat(directory, Not(DirExists()))
        self.assertThat(file_path, Not(FileExists()))

    def test_cleans_up_on_exception_exit(self):
        class DeliberateFailure(Exception):
            pass

        with ExpectedException(DeliberateFailure):
            with tempdir() as directory:
                file_path = factory.make_file(directory)
                raise DeliberateFailure("Exiting context by exception")

        self.assertThat(directory, Not(DirExists()))
        self.assertThat(file_path, Not(FileExists()))

    def test_tolerates_disappearing_dir(self):
        with tempdir() as directory:
            rmtree(directory)

        self.assertThat(directory, Not(DirExists()))

    def test_uses_location(self):
        temp_location = self.make_dir()
        with tempdir(location=temp_location) as directory:
            self.assertThat(directory, DirExists())
            location_listing = os.listdir(temp_location)

        self.assertNotEqual(temp_location, directory)
        self.assertThat(directory, StartsWith(temp_location + os.path.sep))
        self.assertIn(os.path.basename(directory), location_listing)
        self.assertThat(temp_location, DirExists())
        self.assertThat(directory, Not(DirExists()))

    def test_yields_unicode(self):
        with tempdir() as directory:
            pass

        self.assertIsInstance(directory, unicode)

    def test_accepts_unicode_from_mkdtemp(self):
        fake_dir = os.path.join(self.make_dir(), factory.make_name('tempdir'))
        self.assertIsInstance(fake_dir, unicode)
        self.patch(tempfile, 'mkdtemp').return_value = fake_dir

        with tempdir() as directory:
            pass

        self.assertEqual(fake_dir, directory)
        self.assertIsInstance(directory, unicode)

    def test_decodes_bytes_from_mkdtemp(self):
        encoding = 'utf-16'
        self.patch(sys, 'getfilesystemencoding').return_value = encoding
        fake_dir = os.path.join(self.make_dir(), factory.make_name('tempdir'))
        self.patch(tempfile, 'mkdtemp').return_value = fake_dir.encode(
            encoding)
        self.patch(fs_module, 'rmtree')

        with tempdir() as directory:
            pass

        self.assertEqual(fake_dir, directory)
        self.assertIsInstance(directory, unicode)

    def test_uses_prefix(self):
        prefix = factory.make_string(3)
        with tempdir(prefix=prefix) as directory:
            pass

        self.assertThat(os.path.basename(directory), StartsWith(prefix))

    def test_uses_suffix(self):
        suffix = factory.make_string(3)
        with tempdir(suffix=suffix) as directory:
            pass

        self.assertThat(os.path.basename(directory), EndsWith(suffix))

    def test_restricts_access(self):
        with tempdir() as directory:
            mode = os.stat(directory).st_mode
        self.assertEqual(
            stat.S_IMODE(mode),
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


class TestReadTextFile(MAASTestCase):
    def test_reads_file(self):
        text = factory.make_string()
        self.assertEqual(text, read_text_file(self.make_file(contents=text)))

    def test_defaults_to_utf8(self):
        # Test input: "registered trademark" (ringed R) symbol.
        text = '\xae'
        self.assertEqual(
            text,
            read_text_file(self.make_file(contents=text.encode('utf-8'))))

    def test_uses_given_encoding(self):
        # Test input: "registered trademark" (ringed R) symbol.
        text = '\xae'
        self.assertEqual(
            text,
            read_text_file(
                self.make_file(contents=text.encode('utf-16')),
                encoding='utf-16'))


class TestWriteTextFile(MAASTestCase):
    def test_creates_file(self):
        path = os.path.join(self.make_dir(), factory.make_name('text'))
        text = factory.make_string()
        write_text_file(path, text)
        self.assertThat(path, FileContains(text))

    def test_overwrites_file(self):
        path = self.make_file(contents="original text")
        text = factory.make_string()
        write_text_file(path, text)
        self.assertThat(path, FileContains(text))

    def test_defaults_to_utf8(self):
        path = self.make_file()
        # Test input: "registered trademark" (ringed R) symbol.
        text = '\xae'
        write_text_file(path, text)
        self.assertThat(path, FileContains(text.encode('utf-8')))

    def test_uses_given_encoding(self):
        path = self.make_file()
        # Test input: "registered trademark" (ringed R) symbol.
        text = '\xae'
        write_text_file(path, text, encoding='utf-16')
        self.assertThat(path, FileContains(text.encode('utf-16')))


class TestSystemLocks(MAASTestCase):
    """Tests for `SystemLock` and its children."""

    scenarios = (
        ("FileLock", dict(locktype=FileLock)),
        ("RunLock", dict(locktype=RunLock)),
        ("SystemLock", dict(locktype=SystemLock)),
    )

    def make_lock(self):
        lockdir = self.make_dir()
        lockpath = os.path.join(lockdir, factory.make_name("lockfile"))
        lock = self.locktype(lockpath)
        return lockpath, lock

    def ensure_global_lock_held_when_locking_and_unlocking(self, lock):
        # Patch the lock to check that PROCESS_LOCK is held when doing IO.

        def do_lock():
            self.assertTrue(self.locktype.PROCESS_LOCK.locked())
            return True
        self.patch(lock.fslock, "lock").side_effect = do_lock

        def do_unlock():
            self.assertTrue(self.locktype.PROCESS_LOCK.locked())
        self.patch(lock.fslock, "unlock").side_effect = do_unlock

    def test__path_is_read_only(self):
        lockpath, lock = self.make_lock()
        with ExpectedException(AttributeError):
            lock.path = factory.make_name()

    def test__holds_file_system_lock(self):
        _, lock = self.make_lock()
        self.assertFalse(lockfile.isLocked(lock.path))
        with lock:
            self.assertTrue(lockfile.isLocked(lock.path))
        self.assertFalse(lockfile.isLocked(lock.path))

    def test__is_locked_reports_accurately(self):
        lockpath, lock = self.make_lock()
        self.assertFalse(lock.is_locked())
        with lock:
            self.assertTrue(lock.is_locked())
        self.assertFalse(lock.is_locked())

    def test__is_locked_holds_global_lock(self):
        lockpath, lock = self.make_lock()
        PROCESS_LOCK = self.patch(self.locktype, "PROCESS_LOCK")
        self.assertFalse(lock.is_locked())
        self.assertThat(
            PROCESS_LOCK.__enter__,
            MockCalledOnceWith())
        self.assertThat(
            PROCESS_LOCK.__exit__,
            MockCalledOnceWith(None, None, None))

    def test__cannot_be_acquired_twice(self):
        """
        `SystemLock` and its kin do not suffer from a bug that afflicts
        ``lockfile`` (https://pypi.python.org/pypi/lockfile):

          >>> from lockfile import FileLock
          >>> with FileLock('foo'):
          ...     with FileLock('foo'):
          ...         print("Hello!")
          ...
          Hello!
          Traceback (most recent call last):
            File "<stdin>", line 3, in <module>
            File ".../dist-packages/lockfile.py", line 230, in __exit__
              self.release()
            File ".../dist-packages/lockfile.py", line 271, in release
              raise NotLocked
          lockfile.NotLocked

        """
        _, lock = self.make_lock()
        with lock:
            with ExpectedException(self.locktype.NotAvailable, lock.path):
                with lock:
                    pass

    def test__locks_and_unlocks_while_holding_global_lock(self):
        lockpath, lock = self.make_lock()
        self.ensure_global_lock_held_when_locking_and_unlocking(lock)

        with lock:
            self.assertFalse(self.locktype.PROCESS_LOCK.locked())

        self.assertThat(lock.fslock.lock, MockCalledOnceWith())
        self.assertThat(lock.fslock.unlock, MockCalledOnceWith())

    def test__wait_waits_until_lock_can_be_acquired(self):
        clock = self.patch(fs_module, "reactor", Clock())
        sleep = self.patch(fs_module, "sleep")
        sleep.side_effect = clock.advance

        lockpath, lock = self.make_lock()
        do_lock = self.patch(lock.fslock, "lock")
        do_unlock = self.patch(lock.fslock, "unlock")

        do_lock.side_effect = [False, False, True]

        with lock.wait(10):
            self.assertThat(do_lock, MockCallsMatch(call(), call(), call()))
            self.assertThat(sleep, MockCallsMatch(call(1.0), call(1.0)))
            self.assertThat(do_unlock, MockNotCalled())

        self.assertThat(do_unlock, MockCalledOnceWith())

    def test__wait_raises_exception_when_time_has_run_out(self):
        clock = self.patch(fs_module, "reactor", Clock())
        sleep = self.patch(fs_module, "sleep")
        sleep.side_effect = clock.advance

        lockpath, lock = self.make_lock()
        do_lock = self.patch(lock.fslock, "lock")
        do_unlock = self.patch(lock.fslock, "unlock")

        do_lock.return_value = False

        with ExpectedException(self.locktype.NotAvailable):
            with lock.wait(0.2):
                pass

        self.assertThat(do_lock, MockCallsMatch(call(), call(), call()))
        self.assertThat(sleep, MockCallsMatch(call(0.1), call(0.1)))
        self.assertThat(do_unlock, MockNotCalled())

    def test__wait_locks_and_unlocks_while_holding_global_lock(self):
        lockpath, lock = self.make_lock()
        self.ensure_global_lock_held_when_locking_and_unlocking(lock)

        with lock.wait(10):
            self.assertFalse(self.locktype.PROCESS_LOCK.locked())

        self.assertThat(lock.fslock.lock, MockCalledOnceWith())
        self.assertThat(lock.fslock.unlock, MockCalledOnceWith())


class TestSystemLock(MAASTestCase):
    """Tests specific to `SystemLock`."""

    def test__path(self):
        filename = self.make_file()
        observed = SystemLock(filename).path
        self.assertEqual(filename, observed)


class TestFileLock(MAASTestCase):
    """Tests specific to `FileLock`."""

    def test__path(self):
        filename = self.make_file()
        expected = filename + ".lock"
        observed = FileLock(filename).path
        self.assertEqual(expected, observed)


class TestRunLock(MAASTestCase):
    """Tests specific to `RunLock`."""

    def test__path(self):
        filename = self.make_file()
        expected = '/run/lock/maas.' + urlsafe_b64encode(filename) + '.lock'
        observed = RunLock(filename).path
        self.assertEqual(expected, observed)

    def test__uses_utf8_for_unicode_to_byte_conversions(self):
        filename = os.path.abspath(u'\u304b\u3057\u3044')
        path = RunLock(filename).path
        self.addDetail("path", text_content(path))
        self.assertThat(path, IsInstance(bytes))
        path_from_lock = re.search(b'maas[.](.+)[.]lock', path).group(1)
        self.assertThat(
            path_from_lock, MatchesAll(
                Equals(urlsafe_b64encode(filename.encode("utf-8"))),
                IsInstance(bytes)))

    def test__rejects_non_unicode_or_byte_string_in_path(self):
        self.assertRaises(TypeError, RunLock, object())
