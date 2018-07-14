
from urwid import Divider, WEIGHT

from .squeal.database import Database
from .log import make_log
from .uweird.database import SingleTableStatic, DATE_ORDINAL
from .uweird.focus import MessageBar
from .uweird.tabs import TabList
from .uweird.widgets import DividedPile
from .widgets import Definition, App


def make_bound_injury(db, log, tabs, bar, insert_callback=None):
    binder = SingleTableStatic(db, log, 'injury',
                               transforms={'start': DATE_ORDINAL, 'finish': DATE_ORDINAL},
                               insert_callback=insert_callback)
    injury = Definition(log, tabs, bar, binder)
    return injury, binder


def make_widget(db, log, tabs, bar, saves):
    body = []
    for row in db.db.execute('select id, start, finish, title, sort, description from injury'):
        injury, binder = make_bound_injury(db, log, tabs, bar)
        saves.append(binder.save)
        binder.read_row(row)
        body.append(injury)

    pile = DividedPile(body)

    def insert_callback(saves=saves, pile=pile):
        contents = pile.contents
        injury, binder = make_bound_injury(db, log, tabs, bar, insert_callback=insert_callback)
        saves.append(binder.save)
        if contents: contents.append((Divider(), (WEIGHT, 1)))
        contents.append((injury, (WEIGHT, 1)))
        pile.contents = contents

    insert_callback()  # initial empty

    return pile


def main(args):
    log = make_log(args)
    db = Database(args, log)
    tabs = TabList()
    saves = []
    bar = MessageBar()
    injuries = App(log, 'Injuries', bar, make_widget(db, log, tabs, bar, saves), tabs, saves)
    injuries.run()