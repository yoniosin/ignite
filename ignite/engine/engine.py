import inspect
import logging
import sys
import time
from collections import defaultdict
from enum import Enum
import weakref
import numbers

from ignite._utils import _to_hours_mins_secs

IS_PYTHON2 = sys.version_info[0] < 3


class EventWithFilter(object):

    def __init__(self, event, filter):
        if not callable(filter):
            raise TypeError("Argument filter should be callable")
        self.event = event
        self.filter = filter

    def __str__(self):
        return "<%s event=%s, filter=%r>" % (self.__class__.__name__, self.event, self.filter)


class CallableEvents(object):
    """Base class for Events implementing call operator and storing event filter. This class should be inherited
    for any custom events with event filtering feature:

    .. code-block:: python

        from ignite.engine.engine import CallableEvents

        class CustomEvents(CallableEvents, Enum):
            TEST_EVENT = "test_event"

        engine = ...
        engine.register_events(*CustomEvents, event_to_attr={CustomEvents.TEST_EVENT: "test_event"})

        @engine.on(CustomEvents.TEST_EVENT(every=5))
        def call_on_test_event_every(engine):
            # do something

    """
    def __call__(self, event_filter=None, every=None, once=None):

        if not((event_filter is not None) ^ (every is not None) ^ (once is not None)):
            raise ValueError("Only one of the input arguments should be specified")

        if (event_filter is not None) and not callable(event_filter):
            raise TypeError("Argument event_filter should be a callable")

        if (every is not None) and not (isinstance(every, numbers.Integral) and every > 0):
            raise ValueError("Argument every should be integer and greater than zero")

        if (once is not None) and not (isinstance(once, numbers.Integral) and once > 0):
            raise ValueError("Argument every should be integer and positive")

        if every is not None:
            if every == 1:
                # Just return the event itself
                return self
            event_filter = CallableEvents.every_event_filter(every)

        if once is not None:
            event_filter = CallableEvents.once_event_filter(once)

        # check signature:
        Engine._check_signature("engine", event_filter, "event_filter", "event")

        return EventWithFilter(self, event_filter)

    @staticmethod
    def every_event_filter(every):
        def wrapper(engine, event):
            if event % every == 0:
                return True
            return False
        return wrapper

    @staticmethod
    def once_event_filter(once):
        def wrapper(engine, event):
            if event == once:
                return True
            return False
        return wrapper


class Events(CallableEvents, Enum):
    """Events that are fired by the :class:`~ignite.engine.Engine` during execution.

    Since v0.3.0, Events become more flexible and allow to pass an event filter to the Engine:

    .. code-block:: python

        engine = Engine()

        # a) custom event filter
        def custom_event_filter(engine, event):
            if event in [1, 2, 5, 10, 50, 100]:
                return True
            return False

        @engine.on(Events.ITERATION_STARTED(event_filter=custom_event_filter))
        def call_on_special_event(engine):
             # do something on 1, 2, 5, 10, 50, 100 iterations

        # b) "every" event filter
        @engine.on(Events.ITERATION_STARTED(every=10))
        def call_every(engine):
            # do something every 10th iteration

        # c) "once" event filter
        @engine.on(Events.ITERATION_STARTED(once=50))
        def call_once(engine):
            # do something on 50th iteration

    Event filter function `event_filter` accepts as input `engine` and `event` and should return True/False.
    Argument `event` is the value of iteration or epoch, depending on which type of Events the function is passed.

    """
    EPOCH_STARTED = "epoch_started"
    EPOCH_COMPLETED = "epoch_completed"
    STARTED = "started"
    COMPLETED = "completed"
    ITERATION_STARTED = "iteration_started"
    ITERATION_COMPLETED = "iteration_completed"
    EXCEPTION_RAISED = "exception_raised"


class State(object):
    """An object that is used to pass internal and user-defined state between event handlers."""

    event_to_attr = {
        Events.ITERATION_STARTED: "iteration",
        Events.ITERATION_COMPLETED: "iteration",
        Events.EPOCH_STARTED: "epoch",
        Events.EPOCH_COMPLETED: "epoch",
        Events.STARTED: "epoch",
        Events.COMPLETED: "epoch",
    }

    def __init__(self, **kwargs):
        self.output = None
        self.batch = None
        for k, v in kwargs.items():
            setattr(self, k, v)

        for value in self.event_to_attr.values():
            if not hasattr(self, value):
                setattr(self, value, 0)

    def get_event_attrib_value(self, event_name):
        if isinstance(event_name, EventWithFilter):
            event_name = event_name.event
        if event_name not in State.event_to_attr:
            raise RuntimeError("Unknown event name '{}'".format(event_name))
        return getattr(self, State.event_to_attr[event_name])

    def __repr__(self):
        s = "State:\n"
        for attr, value in self.__dict__.items():
            if not isinstance(value, (numbers.Number, str)):
                value = type(value)
            s += "\t{}: {}\n".format(attr, value)
        return s


class RemovableEventHandle(object):
    """A weakref handle to remove a registered event.

    A handle that may be used to remove a registered event handler via the
    remove method, with-statement, or context manager protocol. Returned from
    :meth:`~ignite.engine.Engine.add_event_handler`.


    Args:
        event_name: Registered event name.
        handler: Registered event handler, stored as weakref.
        engine: Target engine, stored as weakref.

    Example usage:

    .. code-block:: python

        engine = Engine()

        def print_epoch(engine):
            print("Epoch: {}".format(engine.state.epoch))

        with engine.add_event_handler(Events.EPOCH_COMPLETED, print_epoch):
            # print_epoch handler registered for a single run
            engine.run(data)

        # print_epoch handler is now unregistered
    """

    def __init__(self, event_name, handler, engine):
        self.event_name = event_name
        self.handler = weakref.ref(handler)
        self.engine = weakref.ref(engine)

    def remove(self):
        """Remove handler from engine."""
        handler = self.handler()
        engine = self.engine()

        if handler is None or engine is None:
            return

        if engine.has_event_handler(handler, self.event_name):
            engine.remove_event_handler(handler, self.event_name)

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.remove()


class Engine(object):
    """Runs a given process_function over each batch of a dataset, emitting events as it goes.

    Args:
        process_function (callable): A function receiving a handle to the engine and the current batch
            in each iteration, and returns data to be stored in the engine's state.

    Attributes:
        state (State): object that is used to pass internal and user-defined state between event handlers.
            It is created and reset on every :meth:`~ignite.engine.Engine.run`.
        last_event_name (Events): last event name triggered by the engine.

    Examples:

        Create a basic trainer

        .. code-block:: python

            def update_model(engine, batch):
                inputs, targets = batch
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()
                return loss.item()

            trainer = Engine(update_model)

            @trainer.on(Events.ITERATION_COMPLETED(every=100))
            def log_training(engine):
                batch_loss = engine.state.output
                lr = optimizer.param_groups[0]['lr']
                e = engine.state.epoch
                n = engine.state.max_epochs
                i = engine.state.iteration
                print("Epoch {}/{} : {} - batch loss: {}, lr: {}".format(e, n, i, batch_loss, lr))

            trainer.run(data_loader, max_epochs=5)

            > Epoch 1/5 : 100 - batch loss: 0.10874069479016124, lr: 0.01
            > ...
            > Epoch 2/5 : 1700 - batch loss: 0.4217900575859437, lr: 0.01

        Create a basic evaluator to compute metrics

        .. code-block:: python
            from ignite.metrics import Accuracy

            def predict_on_batch(engine, batch)
                model.eval()
                with torch.no_grad():
                    x, y = prepare_batch(batch, device=device, non_blocking=non_blocking)
                    y_pred = model(x)

                return y_pred, y

            evaluator = Engine(predict_on_batch)
            Accuracy().attach(evaluator, "val_acc")
            evaluator.run(val_dataloader)

        Compute image mean/std on training dataset

        .. code-block:: python
            from ignite.metrics import Average

            def compute_mean_std(engine, batch):
                b, c, *_ = batch['image'].shape
                data = batch['image'].reshape(b, c, -1).to(dtype=torch.float64)
                mean = torch.mean(data, dim=-1).sum(dim=0)
                mean2 = torch.mean(data ** 2, dim=-1).sum(dim=0)
                return {"mean": mean, "mean^2": mean2}

            compute_engine = Engine(compute_mean_std)
            img_mean = Average(output_transform=lambda output: output['mean'])
            img_mean.attach(compute_engine, 'mean')
            img_mean2 = Average(output_transform=lambda output: output['mean^2'])
            img_mean2.attach(compute_engine, 'mean2')
            state = compute_engine.run(train_loader)
            state.metrics['std'] = torch.sqrt(state.metrics['mean2'] - state.metrics['mean'] ** 2)
            mean = state.metrics['mean'].tolist()
            std = state.metrics['std'].tolist()

    """
    def __init__(self, process_function):
        self._event_handlers = defaultdict(list)
        self.logger = logging.getLogger(__name__ + "." + self.__class__.__name__)
        self.logger.addHandler(logging.NullHandler())
        self._process_function = process_function
        self.last_event_name = None
        self.should_terminate = False
        self.should_terminate_single_epoch = False
        self.state = None
        self._allowed_events = []

        self.register_events(*Events)

        if self._process_function is None:
            raise ValueError("Engine must be given a processing function in order to run.")

        Engine._check_signature(self, process_function, 'process_function', None)

    def register_events(self, *event_names, **kwargs):
        """Add events that can be fired.

        Registering an event will let the user fire these events at any point.
        This opens the door to make the :meth:`~ignite.engine.Engine.run` loop even more
        configurable.

        By default, the events from :class:`~ignite.engine.Events` are registered.

        Args:
            *event_names: An object (ideally a string or int) to define the
                name of the event being supported.
            event_to_attr (dict, optional): A dictionary to map an event to a state attribute.

        Example usage:

        .. code-block:: python

            from enum import Enum
            from ignite.engine import Engine

            class CustomEvents(CallableEvents, Enum):
                FOO_EVENT = "foo_event"
                BAR_EVENT = "bar_event"

            engine = Engine(process_function)
            engine.register_events(*CustomEvents)


        Example with State Attribute:

        .. code-block:: python

            from enum import Enum
            from ignite.engine.engine import Engine, CallableEvents

            class TBPTT_Events(CallableEvents, Enum):
                TIME_ITERATION_STARTED = "time_iteration_started"
                TIME_ITERATION_COMPLETED = "time_iteration_completed"

            TBPTT_event_to_attr = {
                TBPTT_Events.TIME_ITERATION_STARTED: 'time_iteration',
                TBPTT_Events.TIME_ITERATION_COMPLETED: 'time_iteration'
            }

            engine = Engine(process_function)
            engine.register_events(*TBPTT_Events, event_to_attr=TBPTT_event_to_attr)
            engine.run(data)
            # engine.state contains an attribute time_iteration, which can be accessed using engine.state.time_iteration
        """
        # for python2 compatibility:
        event_to_attr = kwargs.get('event_to_attr', None)
        if event_to_attr is not None:
            if not isinstance(event_to_attr, dict):
                raise ValueError('Expected event_to_attr to be dictionary. Got {}.'.format(type(event_to_attr)))

        for e in event_names:
            self._allowed_events.append(e)
            if event_to_attr and e in event_to_attr:
                State.event_to_attr[e] = event_to_attr[e]

    def _handler_wrapper(self, handler, event_name, event_filter):

        def wrapper(*args, **kwargs):
            event = self.state.get_event_attrib_value(event_name)
            if event_filter(self, event):
                return handler(*args, **kwargs)

        return wrapper

    def add_event_handler(self, event_name, handler, priority=0, *args, **kwargs):
        """Add an event handler to be executed when the specified event is fired.

        Args:
            event_name: An event to attach the handler to. Valid events are from :class:`~ignite.engine.Events`
                or any `event_name` added by :meth:`~ignite.engine.Engine.register_events`.
            handler (callable): the callable event handler that should be invoked
            priority(int, optional): change this to determine execution group, Default is 0 (last group)
            *args: optional args to be passed to `handler`.
            **kwargs: optional keyword args to be passed to `handler`.

        Note:
            The handler function's first argument will be `self`, the :class:`~ignite.engine.Engine` object it
            was bound to.

            Note that other arguments can be passed to the handler in addition to the `*args` and  `**kwargs`
            passed here, for example during :attr:`~ignite.engine.Events.EXCEPTION_RAISED`.

        Returns:
            :class:`~ignite.engine.RemovableEventHandler`, which can be used to remove the handler.

        Example usage:

        .. code-block:: python

            engine = Engine(process_function)

            def print_epoch(engine):
                print("Epoch: {}".format(engine.state.epoch))

            engine.add_event_handler(Events.EPOCH_COMPLETED, print_epoch)


        Note:
            Since v0.3.0, Events become more flexible and allow to pass an event filter to the Engine.
            See :class:`~ignite.engine.Events` for more details.

        """
        if isinstance(event_name, EventWithFilter):
            event_name, event_filter = event_name.event, event_name.filter
            handler = self._handler_wrapper(handler, event_name, event_filter)

        if event_name not in self._allowed_events:
            self.logger.error("attempt to add event handler to an invalid event %s.", event_name)
            raise ValueError("Event {} is not a valid event for this Engine.".format(event_name))

        event_args = (Exception(), ) if event_name == Events.EXCEPTION_RAISED else ()
        Engine._check_signature(self, handler, 'handler', *(event_args + args), **kwargs)

        self._event_handlers[event_name].append((priority, handler, args, kwargs))
        self.logger.debug("added handler for event %s.", event_name)

        return RemovableEventHandle(event_name, handler, self)

    def has_event_handler(self, handler, event_name=None):
        """Check if the specified event has the specified handler.

        Args:
            handler (callable): the callable event handler.
            event_name: The event the handler attached to. Set this
                to ``None`` to search all events.
        """
        if event_name is not None:
            if event_name not in self._event_handlers:
                return False
            events = [event_name]
        else:
            events = self._event_handlers
        for e in events:
            for _, h, _, _ in self._event_handlers[e]:
                if h == handler:
                    return True
        return False

    def remove_event_handler(self, handler, event_name):
        """Remove event handler `handler` from registered handlers of the engine

        Args:
            handler (callable): the callable event handler that should be removed
            event_name: The event the handler attached to.

        """
        if event_name not in self._event_handlers:
            raise ValueError("Input event name '{}' does not exist".format(event_name))

        new_event_handlers = [(h, args, kwargs) for h, args, kwargs in self._event_handlers[event_name]
                              if h != handler]
        if len(new_event_handlers) == len(self._event_handlers[event_name]):
            raise ValueError("Input handler '{}' is not found among registered event handlers".format(handler))
        self._event_handlers[event_name] = new_event_handlers

    @staticmethod
    def _check_signature(self, fn, fn_description, *args, **kwargs):
        exception_msg = None

        if IS_PYTHON2:
            try:
                callable_ = fn if hasattr(fn, '__name__') else fn.__call__
                inspect.getcallargs(callable_, self, *args, **kwargs)
            except TypeError as exc:
                spec = inspect.getargspec(callable_)
                fn_params = list(spec.args)
                exception_msg = str(exc)
        else:
            signature = inspect.signature(fn)
            try:
                signature.bind(self, *args, **kwargs)
            except TypeError as exc:
                fn_params = list(signature.parameters)
                exception_msg = str(exc)

        if exception_msg:
            passed_params = [self] + list(args) + list(kwargs)
            raise ValueError("Error adding {} '{}': "
                             "takes parameters {} but will be called with {} "
                             "({}).".format(
                fn, fn_description, fn_params, passed_params, exception_msg))

    def on(self, event_name, priority=0, *args, **kwargs):
        """Decorator shortcut for add_event_handler.

        Args:
            event_name: An event to attach the handler to. Valid events are from :class:`~ignite.engine.Events` or
                any `event_name` added by :meth:`~ignite.engine.Engine.register_events`.
            priority(int, optional): change this to determine execution group, Default is 0 (last group)
            *args: optional args to be passed to `handler`.
            **kwargs: optional keyword args to be passed to `handler`.

        """
        def decorator(f):
            self.add_event_handler(event_name, f, priority, *args, **kwargs)
            return f
        return decorator

    def _sort_handlers(self):
        """sort all handlers list according to their priority, ascending"""
        for key, handlers_list in self._event_handlers.items():
            self._event_handlers[key] = sorted(handlers_list, reverse=True)

    def _fire_event(self, event_name, *event_args, **event_kwargs):
        """Execute all the handlers associated with given event.

        This method executes all handlers associated with the event
        `event_name`. Optional positional and keyword arguments can be used to
        pass arguments to **all** handlers added with this event. These
        arguments updates arguments passed using :meth:`~ignite.engine.Engine.add_event_handler`.

        Args:
            event_name: event for which the handlers should be executed. Valid
                events are from :class:`~ignite.engine.Events` or any `event_name` added by
                :meth:`~ignite.engine.Engine.register_events`.
            *event_args: optional args to be passed to all handlers.
            **event_kwargs: optional keyword args to be passed to all handlers.

        """
        if event_name in self._allowed_events:
            self.logger.debug("firing handlers for event %s ", event_name)
            self.last_event_name = event_name
            for _, func, args, kwargs in self._event_handlers[event_name]:
                kwargs.update(event_kwargs)
                func(self, *(event_args + args), **kwargs)

    def fire_event(self, event_name):
        """Execute all the handlers associated with given event.

        This method executes all handlers associated with the event
        `event_name`. This is the method used in :meth:`~ignite.engine.Engine.run` to call the
        core events found in :class:`~ignite.engine.Events`.

        Custom events can be fired if they have been registered before with
        :meth:`~ignite.engine.Engine.register_events`. The engine `state` attribute should be used
        to exchange "dynamic" data among `process_function` and handlers.

        This method is called automatically for core events. If no custom
        events are used in the engine, there is no need for the user to call
        the method.

        Args:
            event_name: event for which the handlers should be executed. Valid
                events are from :class:`~ignite.engine.Events` or any `event_name` added by
                :meth:`~ignite.engine.Engine.register_events`.

        """
        return self._fire_event(event_name)

    def terminate(self):
        """Sends terminate signal to the engine, so that it terminates completely the run after the current iteration.
        """
        self.logger.info("Terminate signaled. Engine will stop after current iteration is finished.")
        self.should_terminate = True

    def terminate_epoch(self):
        """Sends terminate signal to the engine, so that it terminates the current epoch after the current iteration.
        """
        self.logger.info("Terminate current epoch is signaled. "
                         "Current epoch iteration will stop after current iteration is finished.")
        self.should_terminate_single_epoch = True

    def _run_once_on_dataset(self):
        start_time = time.time()

        try:
            for batch in self.state.dataloader:
                self.state.batch = batch
                self.state.iteration += 1
                self._fire_event(Events.ITERATION_STARTED)
                self.state.output = self._process_function(self, self.state.batch)
                self._fire_event(Events.ITERATION_COMPLETED)
                if self.should_terminate or self.should_terminate_single_epoch:
                    self.should_terminate_single_epoch = False
                    break

        except BaseException as e:
            self.logger.error("Current run is terminating due to exception: %s.", str(e))
            self._handle_exception(e)

        time_taken = time.time() - start_time
        hours, mins, secs = _to_hours_mins_secs(time_taken)

        return hours, mins, secs

    def _handle_exception(self, e):
        if Events.EXCEPTION_RAISED in self._event_handlers:
            self._fire_event(Events.EXCEPTION_RAISED, e)
        else:
            raise e

    def run(self, data, max_epochs=1):
        """Runs the `process_function` over the passed data.

        Args:
            data (Iterable): Collection of batches allowing repeated iteration (e.g., list or `DataLoader`).
            max_epochs (int, optional): max epochs to run for (default: 1).

        Returns:
            State: output state.


        Note:
            User can dynamically preprocess input batch at :attr:`~ignite.engine.Events.ITERATION_STARTED` and store
            output batch in `engine.state.batch`. Latter is passed as usually to `process_function` as argument:

            .. code-block:: python

                trainer = ...

                @trainer.on(Events.ITERATION_STARTED)
                def switch_batch(engine):
                    engine.state.batch = preprocess_batch(engine.state.batch)

        """

        self.state = State(dataloader=data, max_epochs=max_epochs, metrics={})
        self.should_terminate = self.should_terminate_single_epoch = False
        self._sort_handlers()  # sort all handlers at the beginning of a run

        try:
            self.logger.info("Engine run starting with max_epochs={}.".format(max_epochs))
            start_time = time.time()
            self._fire_event(Events.STARTED)
            while self.state.epoch < max_epochs and not self.should_terminate:
                self.state.epoch += 1
                self._fire_event(Events.EPOCH_STARTED)
                hours, mins, secs = self._run_once_on_dataset()
                self.logger.info("Epoch[%s] Complete. Time taken: %02d:%02d:%02d", self.state.epoch, hours, mins, secs)
                if self.should_terminate:
                    break
                self._fire_event(Events.EPOCH_COMPLETED)

            self._fire_event(Events.COMPLETED)
            time_taken = time.time() - start_time
            hours, mins, secs = _to_hours_mins_secs(time_taken)
            self.logger.info("Engine run complete. Time taken %02d:%02d:%02d" % (hours, mins, secs))

        except BaseException as e:
            self.logger.error("Engine run is terminating due to exception: %s.", str(e))
            self._handle_exception(e)

        return self.state

    def setup_logger(self, name=None, handlers_iter=None, logger_level=None):
        """Change the logger's name, configuration and handlers.

        Args:
            name (str, optional): Name to be displayed in the log. If None, will use Ignite's default name.
            handlers_iter (Iterable, optional): Iterator of logging handlers to be added to your logger.
            logger_level (logging level, optional): level of messages to be displayed in the log file.
                If not provided, default is logging.WARNING

        Note:
            When using two engines (i.e trainer and evaluator), use this method to change their names to be able to
            distinguish messages from either one of them.

        """
        if name:
            self.logger.name = name

        if logger_level:
            self.logger.setLevel(logger_level)

        if handlers_iter:
            for handler in handlers_iter:
                self.logger.addHandler(handler)


