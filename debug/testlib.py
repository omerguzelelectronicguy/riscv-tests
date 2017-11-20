import collections
import os
import os.path
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time
import traceback

import pexpect

print_log_names = False
real_stdout = sys.stdout

# Note that gdb comes with its own testsuite. I was unable to figure out how to
# run that testsuite against the spike simulator.

def find_file(path):
    for directory in (os.getcwd(), os.path.dirname(__file__)):
        fullpath = os.path.join(directory, path)
        relpath = os.path.relpath(fullpath)
        if len(relpath) >= len(fullpath):
            relpath = fullpath
        if os.path.exists(relpath):
            return relpath
    return None

def compile(args, xlen=32): # pylint: disable=redefined-builtin
    cc = os.path.expandvars("$RISCV/bin/riscv64-unknown-elf-gcc")
    cmd = [cc, "-g"]
    if xlen == 32:
        cmd.append("-march=rv32imac")
        cmd.append("-mabi=ilp32")
    else:
        cmd.append("-march=rv64imac")
        cmd.append("-mabi=lp64")
    for arg in args:
        found = find_file(arg)
        if found:
            cmd.append(found)
        else:
            cmd.append(arg)
    header("Compile")
    print "+", " ".join(cmd)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if process.returncode:
        print stdout,
        print stderr,
        header("")
        raise Exception("Compile failed!")

class Spike(object):
    def __init__(self, target, halted=False, timeout=None, with_jtag_gdb=True):
        """Launch spike. Return tuple of its process and the port it's running
        on."""
        self.process = None

        if target.harts:
            harts = target.harts
        else:
            harts = [target]

        cmd = self.command(target, harts, halted, timeout, with_jtag_gdb)
        self.infinite_loop = target.compile(harts[0],
                "programs/checksum.c", "programs/tiny-malloc.c",
                "programs/infinite_loop.S", "-DDEFINE_MALLOC", "-DDEFINE_FREE")
        cmd.append(self.infinite_loop)
        self.logfile = tempfile.NamedTemporaryFile(prefix="spike-",
                suffix=".log")
        self.logname = self.logfile.name
        if print_log_names:
            real_stdout.write("Temporary spike log: %s\n" % self.logname)
        self.logfile.write("+ %s\n" % " ".join(cmd))
        self.logfile.flush()
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                stdout=self.logfile, stderr=self.logfile)

        if with_jtag_gdb:
            self.port = None
            for _ in range(30):
                m = re.search(r"Listening for remote bitbang connection on "
                        r"port (\d+).", open(self.logname).read())
                if m:
                    self.port = int(m.group(1))
                    os.environ['REMOTE_BITBANG_PORT'] = m.group(1)
                    break
                time.sleep(0.11)
            if not self.port:
                print_log(self.logname)
                raise Exception("Didn't get spike message about bitbang "
                        "connection")

    def command(self, target, harts, halted, timeout, with_jtag_gdb):
        # pylint: disable=no-self-use
        if target.sim_cmd:
            cmd = shlex.split(target.sim_cmd)
        else:
            spike = os.path.expandvars("$RISCV/bin/spike")
            cmd = [spike]

        cmd += ["-p%d" % len(harts)]

        assert len(set(t.xlen for t in harts)) == 1, \
                "All spike harts must have the same XLEN"

        if harts[0].xlen == 32:
            cmd += ["--isa", "RV32G"]
        else:
            cmd += ["--isa", "RV64G"]

        assert len(set(t.ram for t in harts)) == 1, \
                "All spike harts must have the same RAM layout"
        assert len(set(t.ram_size for t in harts)) == 1, \
                "All spike harts must have the same RAM layout"
        cmd += ["-m0x%x:0x%x" % (harts[0].ram, harts[0].ram_size)]

        if timeout:
            cmd = ["timeout", str(timeout)] + cmd

        if halted:
            cmd.append('-H')
        if with_jtag_gdb:
            cmd += ['--rbb-port', '0']
            os.environ['REMOTE_BITBANG_HOST'] = 'localhost'

        return cmd

    def __del__(self):
        if self.process:
            try:
                self.process.kill()
                self.process.wait()
            except OSError:
                pass

    def wait(self, *args, **kwargs):
        return self.process.wait(*args, **kwargs)

class VcsSim(object):
    logname = "simv.log"

    def __init__(self, sim_cmd=None, debug=False):
        if sim_cmd:
            cmd = shlex.split(sim_cmd)
        else:
            cmd = ["simv"]
        cmd += ["+jtag_vpi_enable"]
        if debug:
            cmd[0] = cmd[0] + "-debug"
            cmd += ["+vcdplusfile=output/gdbserver.vpd"]
        logfile = open(self.logname, "w")
        logfile.write("+ %s\n" % " ".join(cmd))
        logfile.flush()
        listenfile = open(self.logname, "r")
        listenfile.seek(0, 2)
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                stdout=logfile, stderr=logfile)
        done = False
        while not done:
            # Fail if VCS exits early
            exit_code = self.process.poll()
            if exit_code is not None:
                raise RuntimeError('VCS simulator exited early with status %d'
                                   % exit_code)

            line = listenfile.readline()
            if not line:
                time.sleep(1)
            match = re.match(r"^Listening on port (\d+)$", line)
            if match:
                done = True
                self.port = int(match.group(1))
                os.environ['JTAG_VPI_PORT'] = str(self.port)

    def __del__(self):
        try:
            self.process.kill()
            self.process.wait()
        except OSError:
            pass

class Openocd(object):
    logfile = tempfile.NamedTemporaryFile(prefix='openocd', suffix='.log')
    logname = logfile.name

    def __init__(self, server_cmd=None, config=None, debug=False, timeout=60):
        self.timeout = timeout

        if server_cmd:
            cmd = shlex.split(server_cmd)
        else:
            openocd = os.path.expandvars("$RISCV/bin/openocd")
            cmd = [openocd]
            if debug:
                cmd.append("-d")

        # This command needs to come before any config scripts on the command
        # line, since they are executed in order.
        cmd += [
            # Tell OpenOCD to bind gdb to an unused, ephemeral port.
            "--command",
            "gdb_port 0",
            # Disable tcl and telnet servers, since they are unused and because
            # the port numbers will conflict if multiple OpenOCD processes are
            # running on the same server.
            "--command",
            "tcl_port disabled",
            "--command",
            "telnet_port disabled",
        ]

        if config:
            f = find_file(config)
            if f is None:
                print "Unable to read file " + config
                exit(1)

            cmd += ["-f", f]
        if debug:
            cmd.append("-d")

        logfile = open(Openocd.logname, "w")
        if print_log_names:
            real_stdout.write("Temporary OpenOCD log: %s\n" % Openocd.logname)
        logfile.write("+ %s\n" % " ".join(cmd))
        logfile.flush()

        self.gdb_ports = []
        self.process = self.start(cmd, logfile)

    def start(self, cmd, logfile):
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                stdout=logfile, stderr=logfile)

        try:
            # Wait for OpenOCD to have made it through riscv_examine(). When
            # using OpenOCD to communicate with a simulator this may take a
            # long time, and gdb will time out when trying to connect if we
            # attempt too early.
            start = time.time()
            messaged = False
            fd = open(Openocd.logname, "r")
            while True:
                line = fd.readline()
                if not line:
                    if not process.poll() is None:
                        raise Exception("OpenOCD exited early.")
                    time.sleep(0.1)
                    continue

                m = re.search(r"Listening on port (\d+) for gdb connections",
                        line)
                if m:
                    self.gdb_ports.append(int(m.group(1)))

                if "telnet server disabled" in line:
                    return process

                if not messaged and time.time() - start > 1:
                    messaged = True
                    print "Waiting for OpenOCD to start..."
                if (time.time() - start) > self.timeout:
                    raise Exception("Timed out waiting for OpenOCD to "
                            "listen for gdb")

        except Exception:
            print_log(Openocd.logname)
            raise

    def __del__(self):
        try:
            self.process.kill()
            self.process.wait()
        except (OSError, AttributeError):
            pass

class OpenocdCli(object):
    def __init__(self, port=4444):
        self.child = pexpect.spawn(
                "sh -c 'telnet localhost %d | tee openocd-cli.log'" % port)
        self.child.expect("> ")

    def command(self, cmd):
        self.child.sendline(cmd)
        self.child.expect(cmd)
        self.child.expect("\n")
        self.child.expect("> ")
        return self.child.before.strip("\t\r\n \0")

    def reg(self, reg=''):
        output = self.command("reg %s" % reg)
        matches = re.findall(r"(\w+) \(/\d+\): (0x[0-9A-F]+)", output)
        values = {r: int(v, 0) for r, v in matches}
        if reg:
            return values[reg]
        return values

    def load_image(self, image):
        output = self.command("load_image %s" % image)
        if 'invalid ELF file, only 32bits files are supported' in output:
            raise TestNotApplicable(output)

class CannotAccess(Exception):
    def __init__(self, address):
        Exception.__init__(self)
        self.address = address

Thread = collections.namedtuple('Thread', ('id', 'description', 'target_id',
    'name', 'frame'))

class Gdb(object):
    """A single gdb class which can interact with one or more gdb instances."""

    # pylint: disable=too-many-public-methods
    # pylint: disable=too-many-instance-attributes

    def __init__(self, ports,
            cmd=os.path.expandvars("$RISCV/bin/riscv64-unknown-elf-gdb"),
            timeout=60, binary=None):
        assert ports

        self.ports = ports
        self.cmd = cmd
        self.timeout = timeout
        self.binary = binary

        self.stack = []
        self.harts = {}

        self.logfiles = []
        self.children = []
        for port in ports:
            logfile = tempfile.NamedTemporaryFile(prefix="gdb@%d-" % port,
                    suffix=".log")
            self.logfiles.append(logfile)
            if print_log_names:
                real_stdout.write("Temporary gdb log: %s\n" % logfile.name)
            child = pexpect.spawn(cmd)
            child.logfile = logfile
            child.logfile.write("+ %s\n" % cmd)
            self.children.append(child)
        self.active_child = self.children[0]

    def connect(self):
        for port, child in zip(self.ports, self.children):
            self.select_child(child)
            self.wait()
            self.command("set confirm off")
            self.command("set width 0")
            self.command("set height 0")
            # Force consistency.
            self.command("set print entry-values no")
            self.command("set remotetimeout %d" % self.timeout)
            self.command("target extended-remote localhost:%d" % port)
            if self.binary:
                self.command("file %s" % self.binary)
            threads = self.threads()
            for t in threads:
                hartid = None
                if t.name:
                    m = re.search(r"Hart (\d+)", t.name)
                    if m:
                        hartid = int(m.group(1))
                if hartid is None:
                    if self.harts:
                        hartid = max(self.harts) + 1
                    else:
                        hartid = 0
                self.harts[hartid] = (child, t)

    def __del__(self):
        for child in self.children:
            del child

    def lognames(self):
        return [logfile.name for logfile in self.logfiles]

    def select_child(self, child):
        self.active_child = child

    def select_hart(self, hart):
        child, thread = self.harts[hart.id]
        self.select_child(child)
        output = self.command("thread %s" % thread.id)
        assert "Unknown" not in output

    def push_state(self):
        self.stack.append({
            'active_child': self.active_child
            })

    def pop_state(self):
        state = self.stack.pop()
        self.active_child = state['active_child']

    def wait(self):
        """Wait for prompt."""
        self.active_child.expect(r"\(gdb\)")

    def command(self, command, timeout=6000):
        """timeout is in seconds"""
        self.active_child.sendline(command)
        self.active_child.expect("\n", timeout=timeout)
        self.active_child.expect(r"\(gdb\)", timeout=timeout)
        return self.active_child.before.strip()

    def global_command(self, command):
        """Execute this command on every gdb that we control."""
        with PrivateState(self):
            for child in self.children:
                self.select_child(child)
                self.command(command)

    def c(self, wait=True, timeout=-1, async=False):
        """
        Dumb c command.
        In RTOS mode, gdb will resume all harts.
        In multi-gdb mode, this command will just go to the current gdb, so
        will only resume one hart.
        """
        if async:
            async = "&"
        else:
            async = ""
        if wait:
            output = self.command("c%s" % async, timeout=timeout)
            assert "Continuing" in output
            return output
        else:
            self.active_child.sendline("c%s" % async)
            self.active_child.expect("Continuing")

    def c_all(self):
        """
        Resume every hart.

        This function works fine when using multiple gdb sessions, but the
        caller must be careful when using it nonetheless. gdb's behavior is to
        not set breakpoints until just before the hart is resumed, and then
        clears them as soon as the hart halts. That means that you can't set
        one software breakpoint, and expect multiple harts to hit it. It's
        possible that the first hart completes set/run/halt/clear before the
        second hart even gets to resume, so it will never hit the breakpoint.
        """
        with PrivateState(self):
            for child in self.children:
                child.sendline("c")
                child.expect("Continuing")

            # Now wait for them all to halt
            for child in self.children:
                child.expect(r"\(gdb\)")

    def interrupt(self):
        self.active_child.send("\003")
        self.active_child.expect(r"\(gdb\)", timeout=6000)
        return self.active_child.before.strip()

    def x(self, address, size='w'):
        output = self.command("x/%s %s" % (size, address))
        value = int(output.split(':')[1].strip(), 0)
        return value

    def p_raw(self, obj):
        output = self.command("p %s" % obj)
        m = re.search("Cannot access memory at address (0x[0-9a-f]+)", output)
        if m:
            raise CannotAccess(int(m.group(1), 0))
        return output.split('=')[-1].strip()

    def parse_string(self, text):
        text = text.strip()
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1]
            return [self.parse_string(t) for t in inner.split(", ")]
        elif text.startswith('"') and text.endswith('"'):
            return text[1:-1]
        else:
            return int(text, 0)

    def p(self, obj, fmt="/x"):
        output = self.command("p%s %s" % (fmt, obj))
        m = re.search("Cannot access memory at address (0x[0-9a-f]+)", output)
        if m:
            raise CannotAccess(int(m.group(1), 0))
        rhs = output.split('=')[-1]
        return self.parse_string(rhs)

    def p_string(self, obj):
        output = self.command("p %s" % obj)
        value = shlex.split(output.split('=')[-1].strip())[1]
        return value

    def stepi(self):
        output = self.command("stepi", timeout=60)
        return output

    def load(self):
        output = self.command("load", timeout=6000)
        assert "failed" not in  output
        assert "Transfer rate" in output

    def b(self, location):
        output = self.command("b %s" % location)
        assert "not defined" not in output
        assert "Breakpoint" in output
        return output

    def hbreak(self, location):
        output = self.command("hbreak %s" % location)
        assert "not defined" not in output
        assert "Hardware assisted breakpoint" in output
        return output

    def threads(self):
        output = self.command("info threads")
        threads = []
        for line in output.splitlines():
            m = re.match(
                    r"[\s\*]*(\d+)\s*"
                    r"(Remote target|Thread (\d+)\s*\(Name: ([^\)]+))"
                    r"\s*(.*)", line)
            if m:
                threads.append(Thread(*m.groups()))
        assert threads
        #>>>if not threads:
        #>>>    threads.append(Thread('1', '1', 'Default', '???'))
        return threads

    def thread(self, thread):
        return self.command("thread %s" % thread.id)

    def where(self):
        return self.command("where 1")

class PrivateState(object):
    def __init__(self, gdb):
        self.gdb = gdb

    def __enter__(self):
        self.gdb.push_state()

    def __exit__(self, _type, _value, _traceback):
        self.gdb.pop_state()

def run_all_tests(module, target, parsed):
    if not os.path.exists(parsed.logs):
        os.makedirs(parsed.logs)

    overall_start = time.time()

    global gdb_cmd  # pylint: disable=global-statement
    gdb_cmd = parsed.gdb

    todo = []
    examine_added = False
    for hart in target.harts:
        if parsed.misaval:
            hart.misa = int(parsed.misaval, 16)
            print "Using $misa from command line: 0x%x" % hart.misa
        elif hart.misa:
            print "Using $misa from hart definition: 0x%x" % hart.misa
        elif not examine_added:
            todo.append(("ExamineTarget", ExamineTarget, None))
            examine_added = True

    for name in dir(module):
        definition = getattr(module, name)
        if isinstance(definition, type) and hasattr(definition, 'test') and \
                (not parsed.test or any(test in name for test in parsed.test)):
            todo.append((name, definition, None))

    results, count = run_tests(parsed, target, todo)

    header("ran %d tests in %.0fs" % (count, time.time() - overall_start),
            dash=':')

    return print_results(results)

good_results = set(('pass', 'not_applicable'))
def run_tests(parsed, target, todo):
    results = {}
    count = 0

    for name, definition, hart in todo:
        log_name = os.path.join(parsed.logs, "%s-%s-%s.log" %
                (time.strftime("%Y%m%d-%H%M%S"), type(target).__name__, name))
        log_fd = open(log_name, 'w')
        print "[%s] Starting > %s" % (name, log_name)
        instance = definition(target, hart)
        sys.stdout.flush()
        log_fd.write("Test: %s\n" % name)
        log_fd.write("Target: %s\n" % type(target).__name__)
        start = time.time()
        global real_stdout  # pylint: disable=global-statement
        real_stdout = sys.stdout
        sys.stdout = log_fd
        try:
            result = instance.run()
            log_fd.write("Result: %s\n" % result)
        finally:
            sys.stdout = real_stdout
            log_fd.write("Time elapsed: %.2fs\n" % (time.time() - start))
            log_fd.flush()
        print "[%s] %s in %.2fs" % (name, result, time.time() - start)
        if result not in good_results and parsed.print_failures:
            sys.stdout.write(open(log_name).read())
        sys.stdout.flush()
        results.setdefault(result, []).append((name, log_name))
        count += 1
        if result not in good_results and parsed.fail_fast:
            break

    return results, count

def print_results(results):
    result = 0
    for key, value in results.iteritems():
        print "%d tests returned %s" % (len(value), key)
        if key not in good_results:
            result = 1
            for name, log_name in value:
                print "   %s > %s" % (name, log_name)

    return result

def add_test_run_options(parser):
    parser.add_argument("--logs", default="logs",
            help="Store logs in the specified directory.")
    parser.add_argument("--fail-fast", "-f", action="store_true",
            help="Exit as soon as any test fails.")
    parser.add_argument("--print-failures", action="store_true",
            help="When a test fails, print the log file to stdout.")
    parser.add_argument("--print-log-names", "--pln", action="store_true",
            help="Print names of temporary log files as soon as they are "
            "created.")
    parser.add_argument("test", nargs='*',
            help="Run only tests that are named here.")
    parser.add_argument("--gdb",
            help="The command to use to start gdb.")
    parser.add_argument("--misaval",
            help="Don't run ExamineTarget, just assume the misa value which is "
            "specified.")

def header(title, dash='-', length=78):
    if title:
        dashes = dash * (length - 4 - len(title))
        before = dashes[:len(dashes)/2]
        after = dashes[len(dashes)/2:]
        print "%s[ %s ]%s" % (before, title, after)
    else:
        print dash * length

def print_log(path):
    header(path)
    for l in open(path, "r"):
        sys.stdout.write(l)
    print

class BaseTest(object):
    compiled = {}

    def __init__(self, target, hart=None):
        self.target = target
        if hart:
            self.hart = hart
        else:
            self.hart = random.choice(target.harts)
            self.hart = target.harts[-1]    #<<<
        self.server = None
        self.target_process = None
        self.binary = None
        self.start = 0
        self.logs = []

    def early_applicable(self):
        """Return a false value if the test has determined it cannot run
        without ever needing to talk to the target or server."""
        # pylint: disable=no-self-use
        return True

    def setup(self):
        pass

    def compile(self):
        compile_args = getattr(self, 'compile_args', None)
        if compile_args:
            if compile_args not in BaseTest.compiled:
                BaseTest.compiled[compile_args] = \
                        self.target.compile(self.hart, *compile_args)
        self.binary = BaseTest.compiled.get(compile_args)

    def classSetup(self):
        self.compile()
        self.target_process = self.target.create()
        if self.target_process:
            self.logs.append(self.target_process.logname)
        try:
            self.server = self.target.server()
            self.logs.append(self.server.logname)
        except Exception:
            for log in self.logs:
                print_log(log)
            raise

    def classTeardown(self):
        del self.server
        del self.target_process

    def postMortem(self):
        pass

    def run(self):
        """
        If compile_args is set, compile a program and set self.binary.

        Call setup().

        Then call test() and return the result, displaying relevant information
        if an exception is raised.
        """

        sys.stdout.flush()

        if not self.early_applicable():
            return "not_applicable"

        self.start = time.time()

        try:
            self.classSetup()
            self.setup()
            result = self.test()    # pylint: disable=no-member
        except TestNotApplicable:
            result = "not_applicable"
        except Exception as e: # pylint: disable=broad-except
            if isinstance(e, TestFailed):
                result = "fail"
            else:
                result = "exception"
            if isinstance(e, TestFailed):
                header("Message")
                print e.message
            header("Traceback")
            traceback.print_exc(file=sys.stdout)
            try:
                self.postMortem()
            except Exception as e:  # pylint: disable=broad-except
                header("postMortem Exception")
                print e
                traceback.print_exc(file=sys.stdout)
            return result

        finally:
            for log in self.logs:
                print_log(log)
            header("End of logs")
            self.classTeardown()

        if not result:
            result = 'pass'
        return result

gdb_cmd = None
class GdbTest(BaseTest):
    def __init__(self, target, hart=None):
        BaseTest.__init__(self, target, hart=hart)
        self.gdb = None

    def classSetup(self):
        BaseTest.classSetup(self)

        if gdb_cmd:
            self.gdb = Gdb(self.server.gdb_ports, gdb_cmd,
                    timeout=self.target.timeout_sec, binary=self.binary)
        else:
            self.gdb = Gdb(self.server.gdb_ports,
                    timeout=self.target.timeout_sec, binary=self.binary)

        self.logs += self.gdb.lognames()
        self.gdb.connect()

        self.gdb.global_command("set arch riscv:rv%d" % self.hart.xlen)
        self.gdb.global_command("set remotetimeout %d" %
            self.target.timeout_sec)

        for cmd in self.target.gdb_setup:
            self.gdb.command(cmd)

        self.gdb.select_hart(self.hart)

        # FIXME: OpenOCD doesn't handle PRIV now
        #self.gdb.p("$priv=3")

    def postMortem(self):
        if not self.gdb:
            return
        self.gdb.interrupt()
        self.gdb.command("info registers all", timeout=10)

    def classTeardown(self):
        del self.gdb
        BaseTest.classTeardown(self)

class GdbSingleHartTest(GdbTest):
    def classSetup(self):
        GdbTest.classSetup(self)

        for hart in self.target.harts:
            # Park all harts that we're not using in a safe place.
            if hart != self.hart:
                self.gdb.select_hart(hart)
                self.gdb.p("$pc=loop_forever")
        self.gdb.select_hart(self.hart)

class ExamineTarget(GdbTest):
    def test(self):
        for hart in self.target.harts:
            self.gdb.select_hart(hart)

            hart.misa = self.gdb.p("$misa")

            txt = "RV"
            misa_xlen = 0
            if ((hart.misa & 0xFFFFFFFF) >> 30) == 1:
                misa_xlen = 32
            elif ((hart.misa & 0xFFFFFFFFFFFFFFFF) >> 62) == 2:
                misa_xlen = 64
            elif ((hart.misa & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF) >> 126) == 3:
                misa_xlen = 128
            else:
                raise TestFailed("Couldn't determine XLEN from $misa (0x%x)" %
                        self.hart.misa)

            if misa_xlen != hart.xlen:
                raise TestFailed("MISA reported XLEN of %d but we were "\
                        "expecting XLEN of %d\n" % (misa_xlen, hart.xlen))

            txt += ("%d" % misa_xlen)

            for i in range(26):
                if hart.misa & (1<<i):
                    txt += chr(i + ord('A'))
            print txt,

class TestFailed(Exception):
    def __init__(self, message):
        Exception.__init__(self)
        self.message = message

class TestNotApplicable(Exception):
    def __init__(self, message):
        Exception.__init__(self)
        self.message = message

def assertEqual(a, b):
    if a != b:
        raise TestFailed("%r != %r" % (a, b))

def assertNotEqual(a, b):
    if a == b:
        raise TestFailed("%r == %r" % (a, b))

def assertIn(a, b):
    if a not in b:
        raise TestFailed("%r not in %r" % (a, b))

def assertNotIn(a, b):
    if a in b:
        raise TestFailed("%r in %r" % (a, b))

def assertGreater(a, b):
    if not a > b:
        raise TestFailed("%r not greater than %r" % (a, b))

def assertLess(a, b):
    if not a < b:
        raise TestFailed("%r not less than %r" % (a, b))

def assertTrue(a):
    if not a:
        raise TestFailed("%r is not True" % a)

def assertRegexpMatches(text, regexp):
    if not re.search(regexp, text):
        raise TestFailed("can't find %r in %r" % (regexp, text))
