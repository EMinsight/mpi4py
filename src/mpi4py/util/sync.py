# Author:  Lisandro Dalcin
# Contact: dalcinl@gmail.com
"""Synchronization utilities."""
import array as _array
import time as _time
from .. import MPI


__all__ = [
    "Sequential",
    "Counter",
    "Mutex",
    "RMutex",
    "Condition",
]


class Sequential:
    """Sequential execution."""

    def __init__(self, comm, tag=0):
        """Initialize sequential execution.

        Args:
            comm: Intracommunicator context.
            tag: Tag for point-to-point communication.

        """
        self.comm = comm
        self.tag = int(tag)

    def __enter__(self):
        """Enter sequential execution."""
        self.begin()
        return self

    def __exit__(self, *exc):
        """Exit sequential execution."""
        self.end()

    def begin(self):
        """Begin sequential execution."""
        comm = self.comm
        size = comm.Get_size()
        if size == 1:
            return
        rank = comm.Get_rank()
        buf = (bytearray(), 0, MPI.BYTE)
        tag = self.tag
        if rank != 0:
            comm.Recv(buf, rank - 1, tag)

    def end(self):
        """End sequential execution."""
        comm = self.comm
        size = comm.Get_size()
        if size == 1:
            return
        rank = comm.Get_rank()
        buf = (bytearray(), 0, MPI.BYTE)
        tag = self.tag
        if rank != size - 1:
            comm.Send(buf, rank + 1, tag)


class Counter:
    """Parallel counter."""

    def __init__(
        self,
        comm,
        start=0,
        step=1,
        typecode='i',
        root=0,
        info=MPI.INFO_NULL,
    ):
        """Initialize counter object.

        Args:
            comm: Intracommunicator context.
            start: Start value.
            step: Increment value.
            typecode: Type code as defined in the `array` module.
            root: Process rank holding the counter memory.
            info: Info object for RMA context creation.

        """
        # pylint: disable=too-many-arguments
        datatype = MPI.Datatype.fromcode(typecode)
        typechar = datatype.typechar
        rank = comm.Get_rank()
        count = 1 if rank == root else 0
        unitsize = datatype.Get_size()
        window = MPI.Win.Allocate(count * unitsize, unitsize, info, comm)
        self._start = start
        self._step = step
        self._window = window
        self._typechar = typechar
        self._location = (root, 0)

        init = _array.array(typechar, [start] * count)
        window.Lock(rank, MPI.LOCK_SHARED)
        window.Accumulate(init, rank, op=MPI.REPLACE)
        window.Unlock(rank)
        comm.Barrier()

    def __iter__(self):
        """Implement ``iter(self)``."""
        return self

    def __next__(self):
        """Implement ``next(self)``."""
        return self.next()

    def next(self, incr=None):
        """Return current value and increment.

        Args:
            incr: Increment value.

        Returns:
            The counter value before incrementing.

        """
        if not self._window:
            raise RuntimeError("counter already freed")

        window = self._window
        typechar = self._typechar
        rank, disp = self._location

        incr = incr if incr is not None else self._step
        incr = _array.array(typechar, [incr])
        prev = _array.array(typechar, [0])
        window.Lock(rank, MPI.LOCK_SHARED)
        window.Fetch_and_op(incr, prev, rank, disp, MPI.SUM)
        window.Unlock(rank)
        return prev[0]

    def free(self):
        """Free counter resources."""
        window = self._window
        self._window = MPI.WIN_NULL
        window.free()


class Mutex:
    """Parallel mutex."""

    def __init__(self, comm, info=MPI.INFO_NULL):
        """Initialize mutex object.

        Args:
            comm: Intracommunicator context.
            info: Info object for RMA context creation.

        """
        null_rank, tail_rank = MPI.PROC_NULL, 0

        rank = comm.Get_rank()
        count = 3 if rank == tail_rank else 2
        unitsize = MPI.INT.Get_size()
        window = MPI.Win.Allocate(count * unitsize, unitsize, info, comm)
        self._window = window

        init = [False, null_rank, null_rank][:count]
        init = _array.array('i', init)
        window.Lock(rank, MPI.LOCK_SHARED)
        window.Accumulate(init, rank, op=MPI.REPLACE)
        window.Unlock(rank)
        comm.Barrier()

    def _backoff(self):
        backoff = _new_backoff()
        return lambda: next(backoff)

    def _progress(self):
        return lambda: self._window.Flush(self._window.group_rank)

    def _spinloop(self, index, sentinel):
        window = self._window
        memory = memoryview(window).cast('i')
        backoff = self._backoff()
        progress = self._progress()
        window.Sync()
        while memory[index] == sentinel:
            backoff()
            progress()
            window.Sync()
        return memory[index]

    def __enter__(self):
        """Acquire mutex."""
        self.acquire()
        return self

    def __exit__(self, *exc):
        """Release mutex."""
        self.release()

    def acquire(self, blocking=True):
        """Acquire mutex, blocking or non-blocking.

        Args:
            blocking: If `True`, block until the mutex is held.

        Returns:
            `True` if the mutex is held, `False` otherwise.

        """
        null_rank, tail_rank = MPI.PROC_NULL, 0
        lock_id, next_id, tail_id = (0, 1, 2)

        if not self._window:
            raise RuntimeError("mutex already freed")
        if self.locked():
            raise RuntimeError("cannot acquire already held mutex")

        window = self._window
        self_rank = window.group_rank
        window.Lock_all()

        rank = _array.array('i', [self_rank])
        null = _array.array('i', [null_rank])
        prev = _array.array('i', [null_rank])
        window.Accumulate(null, self_rank, next_id, MPI.REPLACE)
        if blocking:
            window.Fetch_and_op(rank, prev, tail_rank, tail_id, MPI.REPLACE)
        else:
            window.Compare_and_swap(rank, null, prev, tail_rank, tail_id)
        window.Flush(tail_rank)
        locked = bool(prev[0] == null_rank)
        if blocking and not locked:
            # Add ourselves to the waiting queue
            window.Accumulate(rank, prev[0], next_id, MPI.REPLACE)
            # Spin until we are given the lock
            locked = bool(self._spinloop(lock_id, 0))

        # Set the local lock flag
        flag = _array.array('i', [locked])
        window.Accumulate(flag, self_rank, lock_id, MPI.REPLACE)

        window.Unlock_all()
        return locked

    def release(self):
        """Release mutex."""
        null_rank, tail_rank = MPI.PROC_NULL, 0
        lock_id, next_id, tail_id = (0, 1, 2)

        if not self._window:
            raise RuntimeError("mutex already freed")
        if not self.locked():
            raise RuntimeError("cannot release unheld mutex")

        window = self._window
        self_rank = window.group_rank
        window.Lock_all()

        rank = _array.array('i', [self_rank])
        null = _array.array('i', [null_rank])
        prev = _array.array('i', [null_rank])
        window.Compare_and_swap(null, rank, prev, tail_rank, tail_id)
        window.Flush(tail_rank)
        if prev[0] != rank[0]:
            # Spin until the next process notify us
            next_rank = self._spinloop(next_id, null_rank)
            # Pass the lock over to the next process
            true = _array.array('i', [True])
            window.Accumulate(true, next_rank, lock_id, MPI.REPLACE)

        # Set the local lock flag
        false = _array.array('i', [False])
        window.Accumulate(false, self_rank, lock_id, MPI.REPLACE)

        window.Unlock_all()

    def locked(self):
        """Return whether the mutex is held."""
        lock_id = 0

        if not self._window:
            raise RuntimeError("mutex already freed")

        memory = memoryview(self._window).cast('i')
        return bool(memory[lock_id])

    def free(self):
        """Free mutex resources."""
        if self._window:
            if self.locked():
                self.release()
        window = self._window
        self._window = MPI.WIN_NULL
        window.free()


class RMutex:
    """Parallel recursive mutex."""

    def __init__(self, comm, info=MPI.INFO_NULL):
        """Initialize recursive mutex object.

        Args:
            comm: Intracommunicator context.
            info: Info object for RMA context creation.

        """
        self._block = Mutex(comm, info)
        self._count = 0

    def __enter__(self):
        """Acquire mutex."""
        self.acquire()
        return self

    def __exit__(self, *exc):
        """Release mutex."""
        self.release()

    def acquire(self, blocking=True):
        """Acquire mutex, blocking or non-blocking.

        Args:
            blocking: If `True`, block until the mutex is held.

        Returns:
            `True` if the mutex is held, `False` otherwise.

        """
        if self._block.locked():
            self._count += 1
            return True
        locked = self._block.acquire(blocking)
        if locked:
            self._count = 1
        return locked

    def release(self):
        """Release mutex."""
        if not self._block.locked():
            raise RuntimeError("cannot release unheld mutex")
        self._count = count = self._count - 1
        if not count:
            self._block.release()

    def locked(self):
        """Return whether the mutex is held."""
        return self._block.locked()

    def count(self):
        """Return recursion count."""
        return self._count

    def free(self):
        """Free mutex resources."""
        self._block.free()
        self._count = 0


class Condition:
    """Parallel condition variable."""

    def __init__(
        self,
        comm,
        lock=None,
        info=MPI.INFO_NULL,
    ):
        """Initialize condition variable object.

        Args:
            comm: Intracommunicator context.
            lock: Basic or recursive mutex object.
            info: Info object for RMA context creation.

        """
        if lock is None:
            self._lock = RMutex(comm, info)
            self._lock_free = self._lock.free
        else:
            self._lock = lock
            self._lock_free = lambda: None

        null_rank, tail_rank = MPI.PROC_NULL, 0

        rank = comm.Get_rank()
        count = 3 if rank == tail_rank else 2
        unitsize = MPI.INT.Get_size()
        window = MPI.Win.Allocate(count * unitsize, unitsize, info, comm)
        self._window = window

        init = [0, null_rank, null_rank][:count]
        init = _array.array('i', init)
        window.Lock(rank, MPI.LOCK_SHARED)
        window.Accumulate(init, rank, op=MPI.REPLACE)
        window.Unlock(rank)
        comm.Barrier()

    def _enqueue(self, process):
        null_rank, tail_rank = MPI.PROC_NULL, 0
        next_id, tail_id = (1, 2)
        window = self._window

        rank = _array.array('i', [process])
        prev = _array.array('i', [null_rank])
        next = _array.array('i', [process])  # pylint: disable=W0622

        window.Lock_all()
        window.Fetch_and_op(rank, prev, tail_rank, tail_id, MPI.REPLACE)
        window.Flush(tail_rank)
        if prev[0] != null_rank:
            window.Fetch_and_op(rank, next, prev[0], next_id, MPI.REPLACE)
            window.Flush(prev[0])
        window.Accumulate(next, rank[0], next_id, MPI.REPLACE)
        window.Unlock_all()

    def _dequeue(self, maxnumprocs):
        null_rank, tail_rank = MPI.PROC_NULL, 0
        next_id, tail_id = (1, 2)
        window = self._window

        null = _array.array('i', [null_rank])
        prev = _array.array('i', [null_rank])
        next = _array.array('i', [null_rank])  # pylint: disable=W0622

        processes = []
        maxnumprocs = max(0, min(maxnumprocs, window.group_size))
        window.Lock_all()
        window.Fetch_and_op(null, prev, tail_rank, tail_id, MPI.NO_OP)
        window.Flush(tail_rank)
        if prev[0] != null_rank:
            empty = False
            window.Fetch_and_op(null, next, prev[0], next_id, MPI.NO_OP)
            window.Flush(prev[0])
            while len(processes) < maxnumprocs and not empty:
                rank = next[0]
                processes.append(rank)
                window.Fetch_and_op(null, next, rank, next_id, MPI.NO_OP)
                window.Flush(rank)
                empty = processes[0] == next[0]
            if not empty:
                window.Accumulate(next, prev[0], next_id, MPI.REPLACE)
            else:
                window.Accumulate(null, tail_rank, tail_id, MPI.REPLACE)
        window.Unlock_all()
        return processes

    def _backoff(self):
        backoff = _new_backoff()
        return lambda: next(backoff)

    def _progress(self):
        return lambda: self._window.Flush(self._window.group_rank)

    def _sleep(self):
        flag_id = 0
        window = self._window
        memory = memoryview(window).cast('i')
        backoff = self._backoff()
        progress = self._progress()
        window.Lock_all()
        window.Sync()
        while memory[flag_id] == 0:
            backoff()
            progress()
            window.Sync()
        memory[flag_id] = 0
        window.Unlock_all()

    def _wakeup(self, processes):
        flag_id = 0
        window = self._window
        flag = _array.array('i', [1])
        window.Lock_all()
        for rank in processes:
            window.Accumulate(flag, rank, flag_id, MPI.REPLACE)
        window.Unlock_all()

    def _release_save(self):
        if isinstance(self._lock, RMutex):
            # pylint: disable=protected-access
            state = self._lock._count
            self._lock._count = 0
            self._lock._block.release()
            return state
        else:
            self._lock.release()
            return None

    def _acquire_restore(self, state):
        if isinstance(self._lock, RMutex):
            # pylint: disable=protected-access
            self._lock._block.acquire()
            self._lock._count = state
        else:
            self._lock.acquire()

    def _lock_reset(self):
        # pylint: disable=protected-access
        if isinstance(self._lock, RMutex):
            self._lock._count = 0
            mutex = self._lock._block
        else:
            mutex = self._lock
        if mutex._window:
            if mutex.locked():
                mutex.release()

    def __enter__(self):
        """Acquire the underlying mutex."""
        self.acquire()
        return self

    def __exit__(self, *exc):
        """Release the underlying mutex."""
        self.release()

    def acquire(self, blocking=True):
        """Acquire the underlying mutex."""
        if not self._window:
            raise RuntimeError("condition already freed")
        return self._lock.acquire(blocking)

    def release(self):
        """Release the underlying mutex."""
        if not self._window:
            raise RuntimeError("condition already freed")
        self._lock.release()

    def locked(self):
        """Return whether the underlying mutex is held."""
        return self._lock.locked()

    def wait(self):
        """Wait until notified by another process.

        Returns:
            Always `True`.

        """
        if not self._window:
            raise RuntimeError("condition already freed")
        if not self.locked():
            raise RuntimeError("cannot wait on unheld mutex")
        self._enqueue(self._window.group_rank)
        state = self._release_save()
        self._sleep()
        self._acquire_restore(state)
        return True

    def wait_for(self, predicate):
        """Wait until a predicate evaluates to `True`.

        Args:
            predicate: callable returning a boolean.

        Returns:
            The result of predicate once it evaluates to `True`.

        """
        result = predicate()
        while not result:
            self.wait()
            result = predicate()
        return result

    def notify(self, n=1):
        """Wake up one or more processes waiting on this condition.

        Args:
            n: Maximum number of processes to wake up.

        Returns:
            The actual number of processes woken up.

        """
        if not self._window:
            raise RuntimeError("condition already freed")
        if not self.locked():
            raise RuntimeError("cannot notify on unheld mutex")
        processes = self._dequeue(n)
        numprocs = len(processes)
        self._wakeup(processes)
        return numprocs

    def notify_all(self):
        """Wake up all processes waiting on this condition.

        Returns:
            The actual number of processes woken up.

        """
        return self.notify((1 << 31) - 1)

    def free(self):
        """Free condition resources."""
        self._lock_reset()
        self._lock_free()
        window = self._window
        self._window = MPI.WIN_NULL
        window.free()


_BACKOFF_DELAY_MAX = 1 / 1024
_BACKOFF_DELAY_MIN = _BACKOFF_DELAY_MAX / 1024
_BACKOFF_DELAY_INIT = 0.0
_BACKOFF_DELAY_RATIO = 2.0


def _new_backoff(
    delay_max=_BACKOFF_DELAY_MAX,
    delay_min=_BACKOFF_DELAY_MIN,
    delay_init=_BACKOFF_DELAY_INIT,
    delay_ratio=_BACKOFF_DELAY_RATIO,
):
    def backoff():
        delay = delay_init
        while True:
            _time.sleep(delay)
            delay = min(delay_max, max(delay_min, delay * delay_ratio))
            yield
    return backoff()
