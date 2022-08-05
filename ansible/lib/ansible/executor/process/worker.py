# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import sys
import traceback

from jinja2.exceptions import TemplateNotFound

from ansible.errors import AnsibleConnectionFailure
from ansible.executor.task_executor import TaskExecutor
from ansible.module_utils._text import to_text
from ansible.utils.display import Display
from ansible.utils.multiprocessing import context as multiprocessing_context

__all__ = ['WorkerProcess']

display = Display()


class WorkerProcess(multiprocessing_context.Process):  # type: ignore[name-defined]
    '''
    The worker thread class, which uses TaskExecutor to run tasks
    read from a job queue and pushes results into a results queue
    for reading later.
    '''

    def __init__(self, final_q, task_vars, host, task, play_context, loader, variable_manager, shared_loader_obj):

        super(WorkerProcess, self).__init__()
        # takes a task queue manager as the sole param:
        self._final_q = final_q
        self._task_vars = task_vars
        self._host = host
        self._task = task
        self._play_context = play_context
        self._loader = loader
        self._variable_manager = variable_manager
        self._shared_loader_obj = shared_loader_obj

        # NOTE: this works due to fork, if switching to threads this should change to per thread storage of temp files
        # clear var to ensure we only delete files for this child
        self._loader._tempfiles = set()

    def _save_stdin(self):
        self._new_stdin = None
        try:
            if sys.stdin.isatty() and sys.stdin.fileno() is not None:
                try:
                    self._new_stdin = os.fdopen(os.dup(sys.stdin.fileno()))
                except OSError:
                    # couldn't dupe stdin, most likely because it's
                    # not a valid file descriptor
                    pass
        except (AttributeError, ValueError):
            # couldn't get stdin's fileno
            pass

        if self._new_stdin is None:
            self._new_stdin = open(os.devnull)

    def start(self):
        '''
        multiprocessing.Process replaces the worker's stdin with a new file
        but we wish to preserve it if it is connected to a terminal.
        Therefore dup a copy prior to calling the real start(),
        ensuring the descriptor is preserved somewhere in the new child, and
        make sure it is closed in the parent when start() completes.
        '''

        self._save_stdin()
        try:
            return super(WorkerProcess, self).start()
        finally:
            self._new_stdin.close()

    def _hard_exit(self, e):
        '''
        There is no safe exception to return to higher level code that does not
        risk an innocent try/except finding itself executing in the wrong
        process. All code executing above WorkerProcess.run() on the stack
        conceptually belongs to another program.
        '''

        try:
            display.debug(u"WORKER HARD EXIT: %s" % to_text(e))
        except BaseException:
            # If the cause of the fault is IOError being generated by stdio,
            # attempting to log a debug message may trigger another IOError.
            # Try printing once then give up.
            pass

        os._exit(1)

    def run(self):
        '''
        Wrap _run() to ensure no possibility an errant exception can cause
        control to return to the StrategyBase task loop, or any other code
        higher in the stack.

        As multiprocessing in Python 2.x provides no protection, it is possible
        a try/except added in far-away code can cause a crashed child process
        to suddenly assume the role and prior state of its parent.
        '''
        try:
            return self._run()
        except BaseException as e:
            self._hard_exit(e)
        finally:
            # This is a hack, pure and simple, to work around a potential deadlock
            # in ``multiprocessing.Process`` when flushing stdout/stderr during process
            # shutdown.
            #
            # We should no longer have a problem with ``Display``, as it now proxies over
            # the queue from a fork. However, to avoid any issues with plugins that may
            # be doing their own printing, this has been kept.
            #
            # This happens at the very end to avoid that deadlock, by simply side
            # stepping it. This should not be treated as a long term fix.
            #
            # TODO: Evaluate migrating away from the ``fork`` multiprocessing start method.
            sys.stdout = sys.stderr = open(os.devnull, 'w')

    def _run(self):
        '''
        Called when the process is started.  Pushes the result onto the
        results queue. We also remove the host from the blocked hosts list, to
        signify that they are ready for their next task.
        '''

        # import cProfile, pstats, StringIO
        # pr = cProfile.Profile()
        # pr.enable()

        # Set the queue on Display so calls to Display.display are proxied over the queue
        display.set_queue(self._final_q)

        try:
            # execute the task and build a TaskResult from the result
            display.debug("running TaskExecutor() for %s/%s" % (self._host, self._task))
            executor_result = TaskExecutor(
                self._host,
                self._task,
                self._task_vars,
                self._play_context,
                self._new_stdin,
                self._loader,
                self._shared_loader_obj,
                self._final_q
            ).run()

            display.debug("done running TaskExecutor() for %s/%s [%s]" % (self._host, self._task, self._task._uuid))
            self._host.vars = dict()
            self._host.groups = []

            # put the result on the result queue
            display.debug("sending task result for task %s" % self._task._uuid)
            self._final_q.send_task_result(
                self._host.name,
                self._task._uuid,
                executor_result,
                task_fields=self._task.dump_attrs(),
            )
            display.debug("done sending task result for task %s" % self._task._uuid)

        except AnsibleConnectionFailure:
            self._host.vars = dict()
            self._host.groups = []
            self._final_q.send_task_result(
                self._host.name,
                self._task._uuid,
                dict(unreachable=True),
                task_fields=self._task.dump_attrs(),
            )

        except Exception as e:
            if not isinstance(e, (IOError, EOFError, KeyboardInterrupt, SystemExit)) or isinstance(e, TemplateNotFound):
                try:
                    self._host.vars = dict()
                    self._host.groups = []
                    self._final_q.send_task_result(
                        self._host.name,
                        self._task._uuid,
                        dict(failed=True, exception=to_text(traceback.format_exc()), stdout=''),
                        task_fields=self._task.dump_attrs(),
                    )
                except Exception:
                    display.debug(u"WORKER EXCEPTION: %s" % to_text(e))
                    display.debug(u"WORKER TRACEBACK: %s" % to_text(traceback.format_exc()))
                finally:
                    self._clean_up()

        display.debug("WORKER PROCESS EXITING")

        # pr.disable()
        # s = StringIO.StringIO()
        # sortby = 'time'
        # ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        # ps.print_stats()
        # with open('worker_%06d.stats' % os.getpid(), 'w') as f:
        #     f.write(s.getvalue())

    def _clean_up(self):
        # NOTE: see note in init about forks
        # ensure we cleanup all temp files for this worker
        self._loader.cleanup_all_tmp_files()
