# -*- coding: utf-8 -*-
"""IPython Test Process Controller

This module runs one or more subprocesses which will actually run the IPython
test suite.

"""

#-----------------------------------------------------------------------------
#  Copyright (C) 2009-2011  The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------
from __future__ import print_function

import argparse
import multiprocessing.pool
from multiprocessing import Process, Queue
import os
import shutil
import signal
import sys
import subprocess
import time

from .iptest import have, test_group_names as py_test_group_names, test_sections
from IPython.utils.path import compress_user
from IPython.utils.py3compat import bytes_to_str
from IPython.utils.sysinfo import get_sys_info
from IPython.utils.tempdir import TemporaryDirectory


class TestController(object):
    """Run tests in a subprocess
    """
    #: str, IPython test suite to be executed.
    section = None
    #: list, command line arguments to be executed
    cmd = None
    #: dict, extra environment variables to set for the subprocess
    env = None
    #: list, TemporaryDirectory instances to clear up when the process finishes
    dirs = None
    #: subprocess.Popen instance
    process = None
    #: str, process stdout+stderr
    stdout = None
    #: bool, whether to capture process stdout & stderr
    buffer_output = False

    def __init__(self):
        self.cmd = []
        self.env = {}
        self.dirs = []

    def launch(self):
        # print('*** ENV:', self.env)  # dbg
        # print('*** CMD:', self.cmd)  # dbg
        env = os.environ.copy()
        env.update(self.env)
        output = subprocess.PIPE if self.buffer_output else None
        stdout = subprocess.STDOUT if self.buffer_output else None
        self.process = subprocess.Popen(self.cmd, stdout=output,
                stderr=stdout, env=env)

    def wait(self):
        self.stdout, _ = self.process.communicate()
        return self.process.returncode

    def cleanup_process(self):
        """Cleanup on exit by killing any leftover processes."""
        subp = self.process
        if subp is None or (subp.poll() is not None):
            return  # Process doesn't exist, or is already dead.

        try:
            print('Cleaning up stale PID: %d' % subp.pid)
            subp.kill()
        except: # (OSError, WindowsError) ?
            # This is just a best effort, if we fail or the process was
            # really gone, ignore it.
            pass
        else:
            for i in range(10):
                if subp.poll() is None:
                    time.sleep(0.1)
                else:
                    break

        if subp.poll() is None:
            # The process did not die...
            print('... failed. Manual cleanup may be required.')

    def cleanup(self):
        "Kill process if it's still alive, and clean up temporary directories"
        self.cleanup_process()
        for td in self.dirs:
            td.cleanup()

    __del__ = cleanup

class PyTestController(TestController):
    """Run Python tests using IPython.testing.iptest"""
    #: str, Python command to execute in subprocess
    pycmd = None

    def __init__(self, section):
        """Create new test runner."""
        TestController.__init__(self)
        self.section = section
        # pycmd is put into cmd[2] in PyTestController.launch()
        self.cmd = [sys.executable, '-c', None, section]
        self.pycmd = "from IPython.testing.iptest import run_iptest; run_iptest()"
        ipydir = TemporaryDirectory()
        self.dirs.append(ipydir)
        self.env['IPYTHONDIR'] = ipydir.name
        self.workingdir = workingdir = TemporaryDirectory()
        self.dirs.append(workingdir)
        self.env['IPTEST_WORKING_DIR'] = workingdir.name
        # This means we won't get odd effects from our own matplotlib config
        self.env['MPLCONFIGDIR'] = workingdir.name

    @property
    def will_run(self):
        try:
            return test_sections[self.section].will_run
        except KeyError:
            return True

    def add_xunit(self):
        xunit_file = os.path.abspath(self.section + '.xunit.xml')
        self.cmd.extend(['--with-xunit', '--xunit-file', xunit_file])

    def add_coverage(self):
        try:
            sources = test_sections[self.section].includes
        except KeyError:
            sources = ['IPython']

        coverage_rc = ("[run]\n"
                       "data_file = {data_file}\n"
                       "source =\n"
                       "  {source}\n"
                      ).format(data_file=os.path.abspath('.coverage.'+self.section),
                               source="\n  ".join(sources))
        config_file = os.path.join(self.workingdir.name, '.coveragerc')
        with open(config_file, 'w') as f:
            f.write(coverage_rc)

        self.env['COVERAGE_PROCESS_START'] = config_file
        self.pycmd = "import coverage; coverage.process_startup(); " + self.pycmd

    def launch(self):
        self.cmd[2] = self.pycmd
        super(PyTestController, self).launch()

class JSController(TestController):
    """Run CasperJS tests """
    def __init__(self, section):
        """Create new test runner."""
        TestController.__init__(self)
        self.section = section

        self.ipydir = TemporaryDirectory()
        self.dirs.append(self.ipydir)
        self.env['IPYTHONDIR'] = self.ipydir.name

    def launch(self):
        # start the ipython notebook, so we get the port number
        self._init_server()

        import IPython.html.tests as t
        test_dir = os.path.join(os.path.dirname(t.__file__), 'casperjs')
        includes = '--includes=' + os.path.join(test_dir,'util.js')
        test_cases = os.path.join(test_dir, 'test_cases')
        port = '--port=' + str(self.server_port)
        self.cmd = ['casperjs', 'test', port, includes, test_cases]

        super(JSController, self).launch()

    @property
    def will_run(self):
        return all(have[a] for a in ['zmq', 'tornado', 'jinja2', 'casperjs'])

    def _init_server(self):
        "Start the notebook server in a separate process"
        self.queue = q = Queue()
        self.server = Process(target=run_webapp, args=(q, self.ipydir.name))
        self.server.start()
        self.server_port = q.get()

    def cleanup(self):
        self.server.terminate()
        self.server.join()
        TestController.cleanup(self)

js_test_group_names = {'js'}

def run_webapp(q, nbdir, loglevel=0):
    """start the IPython Notebook, and pass port back to the queue"""
    import os
    import IPython.html.notebookapp as nbapp
    import sys
    sys.stderr = open(os.devnull, 'w')
    os.environ["IPYTHONDIR"] = nbdir
    server = nbapp.NotebookApp()
    args = ['--no-browser']
    args.append('--notebook-dir='+nbdir)
    args.append('--profile-dir='+nbdir)
    args.append('--log-level='+str(loglevel))
    server.initialize(args)
    # communicate the port number to the parent process
    q.put(server.port)
    server.start()

def prepare_controllers(options):
    """Returns two lists of TestController instances, those to run, and those
    not to run."""
    testgroups = options.testgroups

    if testgroups:
        py_testgroups = [g for g in testgroups if g in py_test_group_names]
        js_testgroups = [g for g in testgroups if g in js_test_group_names]
    else:
        py_testgroups = py_test_group_names
        js_testgroups = js_test_group_names
        if not options.all:
            test_sections['parallel'].enabled = False

    c_js = [JSController(name) for name in js_testgroups]
    c_py = [PyTestController(name) for name in py_testgroups]

    configure_py_controllers(c_py, xunit=options.xunit,
            coverage=options.coverage)

    controllers = c_py + c_js
    to_run = [c for c in controllers if c.will_run]
    not_run = [c for c in controllers if not c.will_run]
    return to_run, not_run

def configure_py_controllers(controllers, xunit=False, coverage=False, extra_args=()):
    """Apply options for a collection of TestController objects."""
    for controller in controllers:
        if xunit:
            controller.add_xunit()
        if coverage:
            controller.add_coverage()
        controller.cmd.extend(extra_args)

def do_run(controller):
    try:
        try:
            controller.launch()
        except Exception:
            import traceback
            traceback.print_exc()
            return controller, 1  # signal failure

        exitcode = controller.wait()
        return controller, exitcode

    except KeyboardInterrupt:
        return controller, -signal.SIGINT
    finally:
        controller.cleanup()

def report():
    """Return a string with a summary report of test-related variables."""
    inf = get_sys_info()
    out = []
    def _add(name, value):
        out.append((name, value))

    _add('IPython version', inf['ipython_version'])
    _add('IPython commit', "{} ({})".format(inf['commit_hash'], inf['commit_source']))
    _add('IPython package', compress_user(inf['ipython_path']))
    _add('Python version', inf['sys_version'].replace('\n',''))
    _add('sys.executable', compress_user(inf['sys_executable']))
    _add('Platform', inf['platform'])

    width = max(len(n) for (n,v) in out)
    out = ["{:<{width}}: {}\n".format(n, v, width=width) for (n,v) in out]

    avail = []
    not_avail = []

    for k, is_avail in have.items():
        if is_avail:
            avail.append(k)
        else:
            not_avail.append(k)

    if avail:
        out.append('\nTools and libraries available at test time:\n')
        avail.sort()
        out.append('   ' + ' '.join(avail)+'\n')

    if not_avail:
        out.append('\nTools and libraries NOT available at test time:\n')
        not_avail.sort()
        out.append('   ' + ' '.join(not_avail)+'\n')

    return ''.join(out)

def run_iptestall(options):
    """Run the entire IPython test suite by calling nose and trial.

    This function constructs :class:`IPTester` instances for all IPython
    modules and package and then runs each of them.  This causes the modules
    and packages of IPython to be tested each in their own subprocess using
    nose.

    Parameters
    ----------

    All parameters are passed as attributes of the options object.

    testgroups : list of str
      Run only these sections of the test suite. If empty, run all the available
      sections.

    fast : int or None
      Run the test suite in parallel, using n simultaneous processes. If None
      is passed, one process is used per CPU core. Default 1 (i.e. sequential)

    inc_slow : bool
      Include slow tests, like IPython.parallel. By default, these tests aren't
      run.

    xunit : bool
      Produce Xunit XML output. This is written to multiple foo.xunit.xml files.

    coverage : bool or str
      Measure code coverage from tests. True will store the raw coverage data,
      or pass 'html' or 'xml' to get reports.

    extra_args : list
      Extra arguments to pass to the test subprocesses, e.g. '-v'
    """
    if options.fast != 1:
        # If running in parallel, capture output so it doesn't get interleaved
        TestController.buffer_output = True

    to_run, not_run = prepare_controllers(options)

    def justify(ltext, rtext, width=70, fill='-'):
        ltext += ' '
        rtext = (' ' + rtext).rjust(width - len(ltext), fill)
        return ltext + rtext

    # Run all test runners, tracking execution time
    failed = []
    t_start = time.time()

    print()
    if options.fast == 1:
        # This actually means sequential, i.e. with 1 job
        for controller in to_run:
            print('IPython test group:', controller.section)
            sys.stdout.flush()  # Show in correct order when output is piped
            controller, res = do_run(controller)
            if res:
                failed.append(controller)
                if res == -signal.SIGINT:
                    print("Interrupted")
                    break
            print()

    else:
        # Run tests concurrently
        try:
            pool = multiprocessing.pool.ThreadPool(options.fast)
            for (controller, res) in pool.imap_unordered(do_run, to_run):
                res_string = 'OK' if res == 0 else 'FAILED'
                print(justify('IPython test group: ' + controller.section, res_string))
                if res:
                    print(bytes_to_str(controller.stdout))
                    failed.append(controller)
                    if res == -signal.SIGINT:
                        print("Interrupted")
                        break
        except KeyboardInterrupt:
            return

    for controller in not_run:
        print(justify('IPython test group: ' + controller.section, 'NOT RUN'))

    t_end = time.time()
    t_tests = t_end - t_start
    nrunners = len(to_run)
    nfail = len(failed)
    # summarize results
    print('_'*70)
    print('Test suite completed for system with the following information:')
    print(report())
    took = "Took %.3fs." % t_tests
    print('Status: ', end='')
    if not failed:
        print('OK (%d test groups).' % nrunners, took)
    else:
        # If anything went wrong, point out what command to rerun manually to
        # see the actual errors and individual summary
        failed_sections = [c.section for c in failed]
        print('ERROR - {} out of {} test groups failed ({}).'.format(nfail,
                                  nrunners, ', '.join(failed_sections)), took)
        print()
        print('You may wish to rerun these, with:')
        print('  iptest', *failed_sections)
        print()

    if options.coverage:
        from coverage import coverage
        cov = coverage(data_file='.coverage')
        cov.combine()
        cov.save()

        # Coverage HTML report
        if options.coverage == 'html':
            html_dir = 'ipy_htmlcov'
            shutil.rmtree(html_dir, ignore_errors=True)
            print("Writing HTML coverage report to %s/ ... " % html_dir, end="")
            sys.stdout.flush()

            # Custom HTML reporter to clean up module names.
            from coverage.html import HtmlReporter
            class CustomHtmlReporter(HtmlReporter):
                def find_code_units(self, morfs):
                    super(CustomHtmlReporter, self).find_code_units(morfs)
                    for cu in self.code_units:
                        nameparts = cu.name.split(os.sep)
                        if 'IPython' not in nameparts:
                            continue
                        ix = nameparts.index('IPython')
                        cu.name = '.'.join(nameparts[ix:])

            # Reimplement the html_report method with our custom reporter
            cov._harvest_data()
            cov.config.from_args(omit='*%stests' % os.sep, html_dir=html_dir,
                                 html_title='IPython test coverage',
                                )
            reporter = CustomHtmlReporter(cov, cov.config)
            reporter.report(None)
            print('done.')

        # Coverage XML report
        elif options.coverage == 'xml':
            cov.xml_report(outfile='ipy_coverage.xml')

    if failed:
        # Ensure that our exit code indicates failure
        sys.exit(1)


def main():
    # Arguments after -- should be passed through to nose. Argparse treats
    # everything after -- as regular positional arguments, so we separate them
    # first.
    try:
        ix = sys.argv.index('--')
    except ValueError:
        to_parse = sys.argv[1:]
        extra_args = []
    else:
        to_parse = sys.argv[1:ix]
        extra_args = sys.argv[ix+1:]

    parser = argparse.ArgumentParser(description='Run IPython test suite')
    parser.add_argument('testgroups', nargs='*',
                        help='Run specified groups of tests. If omitted, run '
                        'all tests.')
    parser.add_argument('--all', action='store_true',
                        help='Include slow tests not run by default.')
    parser.add_argument('-j', '--fast', nargs='?', const=None, default=1, type=int,
                        help='Run test sections in parallel.')
    parser.add_argument('--xunit', action='store_true',
                        help='Produce Xunit XML results')
    parser.add_argument('--coverage', nargs='?', const=True, default=False,
                        help="Measure test coverage. Specify 'html' or "
                        "'xml' to get reports.")

    options = parser.parse_args(to_parse)
    options.extra_args = extra_args

    run_iptestall(options)


if __name__ == '__main__':
    main()
