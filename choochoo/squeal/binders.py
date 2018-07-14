from sqlalchemy import inspect, Text, TEXT, ColumnDefault
from urwid import connect_signal, disconnect_signal


class Binder:

    # this works in two ways:
    # 1 - a single row is updated.  key values must be supplied in defaults and composite
    #     keys are possible, but modifying the key values raises an exception
    # 2 - rows can be navigated.  a single primary key only.  when changed, the session
    #     is committed and a new value read from the database.

    def __init__(self, log, session, widget, table, multirow=False, defaults=None):
        if defaults is None: defaults = {}
        self.__log = log
        self.__session = session
        self.__widget = widget
        self.__table = table
        self.__multirow = multirow
        self.__defaults = defaults
        self.__primary_keys = tuple(map(lambda column: column.name, inspect(table).primary_key))
        if self.__multirow and len(self.__primary_keys) > 1:
            raise Exception('Composite key not compatible with multirow')
        self.instance = None
        self.__read()
        self.__bind()

    def __bind(self):
        self.__log.debug('Binding %s to %s' % (self.instance, self.__widget))
        for column in inspect(self.__table).columns:
            name = column.name
            value = getattr(self.instance, name)
            if not column.nullable and value is None:
                value = column.default.arg
                setattr(self.instance, name, value)
            if not name.startswith('_'):
                try:
                    widget = getattr(self.__widget, name)
                    self.__log.debug('Setting %s=%s on %s' % (name, value, widget))
                    while widget:
                        if hasattr(widget, 'state'):
                            self.__bind_state(name, value, widget)
                            break
                        elif hasattr(widget, 'set_edit_text'):
                            self.__bind_edit(name, value, widget)
                            break
                        elif hasattr(widget, 'base_widget') and widget != widget.base_widget:
                            widget = widget.base_widget
                        elif hasattr(widget, '_wrapped_widget') and widget != widget._wrapped_widget:
                            widget = widget._wrapped_widget
                        else:
                            self.__log.error('Cannot set %s on %s (%s)' % (name, widget, dir(widget)))
                            break
                except AttributeError as e:
                    self.__log.warn('Cannot find %s member of %s (%s): %s' %
                                    (name, self.__widget, dir(self.__widget), e))

    def __bind_state(self, name, value, widget):
        widget.state = value
        self.__connect(widget, name)

    def __bind_edit(self, name, value, widget):
        widget.set_edit_text(value)
        self.__connect(widget, name)

    def __connect(self, widget, name):
        # we don't use weak args because we want the bineder to be around as long as the widget
        user_args = [name]
        self.__log.debug('Disconnecting %s' % widget)
        disconnect_signal(widget, 'change', self.__change_callback, user_args=user_args)
        self.__log.debug('Connecting %s' % widget)
        connect_signal(widget, 'change', self.__change_callback, user_args=user_args)

    def __change_callback(self, name, widget, value):
        self.__log.debug('Change %s=%s for %s (%s)' % (name, value, widget, self.instance))
        if name in self.__primary_keys:
            if self.__multirow:
                self.__session.commit()
                self.__defaults[name] = value
                self.__read()
                self.__bind()
            elif value != getattr(self.instance, name):
                raise Exception('Primary key (%s) modified, but not multirow')
        else:
            self.__log.debug('Setting %s=%s on %s' % (name, value, self.instance))
            setattr(self.instance, name, value)

    def __read(self):
        self.__log.debug('Reading new %s' % self.__table)
        query = self.__session.query(self.__table)
        for (k, v) in self.__defaults.items():
            query = query.filter(getattr(self.__table, k) == v)
        self.instance = query.one_or_none()
        if not self.instance:
            self.__log.debug("No data read, so creating default from %s" % self.__defaults)
            self.instance = self.__table(**self.__defaults)
            self.__session.add(self.instance)