
from abc import abstractmethod
from enum import IntEnum

from sqlalchemy import ForeignKey, Column, Integer, func, and_, UniqueConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Session, aliased

from ..support import Base
from ..types import OpenSched, Date, ShortCls, short_cls
from ...lib.date import to_time, time_to_local_date, max_time, min_time, extend_range


class SourceType(IntEnum):

    SOURCE = 0
    INTERVAL = 1
    ACTIVITY = 2
    TOPIC = 3
    CONSTANT = 4
    MONITOR = 5


class Source(Base):

    __tablename__ = 'source'

    id = Column(Integer, primary_key=True)
    type = Column(Integer, nullable=False, index=True)  # index needed for fast deletes of subtypes

    __mapper_args__ = {
        'polymorphic_identity': SourceType.SOURCE,
        'polymorphic_on': type
    }

    @abstractmethod
    def time_range(self, s):
        raise NotImplementedError('time_range for %s' % self)

    @classmethod
    def before_flush(cls, s):
        cls.__clean_dirty_intervals(s)

    @classmethod
    def __clean_dirty_intervals(cls, s):
        from .statistic import StatisticJournal
        # sessions are generally restricted to one time region, so we'll bracket that rather
        # than list all times
        start, finish = None, None
        # all sources except intervals that are being deleted (need to catch on cascade to statistics)
        for instance in s.deleted:
            if isinstance(instance, Source) and not isinstance(instance, Interval):
                a, b = instance.time_range(s)
                start, finish = min_time(a, start), max_time(b, finish)
        # all modified statistics
        for instance in s.dirty:
            # ignore constants as time 0
            if isinstance(instance, StatisticJournal) and s.is_modified(instance) and instance.time:
                start, finish = extend_range(start, finish, instance.time)
        # all new statistics that aren't associated with intervals and have non-null data
        # (avoid triggering on empty diary entries)
        for instance in s.new:
            # ignore constants as time 0
            if isinstance(instance, StatisticJournal) and not isinstance(instance.source, Interval) \
                    and instance.value is not None and instance.time:
                start, finish = extend_range(start, finish, instance.time)
        if start is not None:
            Interval.clean_times(s, start, finish)


@listens_for(Session, 'before_flush')
def before_flush(session, context, instances):
    Source.before_flush(session)


class NoStatistics(Exception):
    pass


class Interval(Source):

    __tablename__ = 'interval'

    id = Column(Integer, ForeignKey('source.id', ondelete='cascade'), primary_key=True)
    schedule = Column(OpenSched, nullable=False, index=True)
    # disambiguate creator so each can wipe only its own data on force
    owner = Column(ShortCls, nullable=False)
    # these are for the schedule - finish is redundant (start is not because of timezone issues)
    start = Column(Date, nullable=False, index=True)
    finish = Column(Date, nullable=False, index=True)
    UniqueConstraint(schedule, owner, start)

    __mapper_args__ = {
        'polymorphic_identity': SourceType.INTERVAL
    }

    def __str__(self):
        owner = self.owner if isinstance(self.owner, str) else short_cls(self.owner)
        return 'Interval "%s from %s" (owner %s)' % (self.schedule, self.start, owner)

    @classmethod
    def _missing_interval_start_dates(cls, log, s, schedule, interval_owner, statistic_owner=None):
        '''
        Returns a list of times that mark the start DATES of missing intervals covering "all"
        statistics (optionally with a given owner and excluding constants at "zero time"),
        plus the end TIME of the statistics,
        '''
        stats_start, stats_finish = cls._raw_statistics_time_range(s, statistic_owner)
        log.debug('Statistics exist %s - %s' % (stats_start, stats_finish))
        starts = cls._open_interval_dates(s, schedule, interval_owner)
        stats_start_date = schedule.start_of_frame(time_to_local_date(stats_start))
        if not starts or starts[0] > stats_start_date:
            if not cls._existing_interval_at(s, schedule, interval_owner, stats_start_date):
                starts = [stats_start_date] + starts
        log.debug('Have %d open blocks finishing at %s' % (len(starts), stats_finish))
        for i, start in enumerate(starts):
            log.debug('Block %d starts at %s' % (i, start))
        log.debug('')
        return starts, stats_finish

    @classmethod
    def _raw_statistics_time_range(cls, s, statistics_owner=None):
        '''
        The time range over which statistics exist (optionally restricted by owner),
        ignoring constants are "time zero".  This is the first to the last time for
        any statistics - it pays no attention to gaps.
        '''
        from .statistic import StatisticJournal, StatisticName
        q = s.query(func.min(StatisticJournal.time), func.max(StatisticJournal.time)). \
            filter(StatisticJournal.time > to_time(24 * 60 * 60.0))
        if statistics_owner:
            q = q.join(StatisticName).filter(StatisticName.owner == statistics_owner)
        start, finish = q.one()   # skip entire first day because tz
        if start and finish:
            return start, finish
        else:
            raise NoStatistics('No statistics are currently defined')

    @classmethod
    def _open_interval_dates(cls, s, schedule, interval_owner):
        '''
        The finish DATES that come at the ends of contiguous intervals
        (for intervals of the given schedule and owner)
        '''
        close = aliased(Interval)
        return [result[0] for result in s.query(Interval.finish). \
            outerjoin(close,
                      and_(Interval.finish == close.start,
                           Interval.owner == close.owner,
                           Interval.schedule == close.schedule)). \
            filter(close.start == None,
                   Interval.owner == interval_owner,
                   Interval.schedule == schedule). \
            order_by(Interval.finish).all()]

    @classmethod
    def _existing_interval_at(cls, s, schedule, interval_owner, start):
        '''
        The existing interval for a given start, owner, schedule.
        '''
        return s.query(Interval). \
            filter(Interval.start == start,
                   Interval.schedule == schedule,
                   Interval.owner == interval_owner).one_or_none()

    @classmethod
    def _missing_interval_dates_from(cls, log, s, schedule, interval_owner, start, finish):
        '''
        Iterator over start DATES for intervals that are not present in the database,
        starting from `start` and ending when an interval is found, or with `finish` if no intervals are found.
        '''
        while start <= time_to_local_date(finish):
            next = schedule.next_frame(start)
            log.debug('Missing Interval %s - %s' % (start, next))
            yield start, next
            start = next
            if cls._existing_interval_at(s, schedule, interval_owner, start):
                return

    @classmethod
    def first_missing_date(cls, log, s, schedule, interval_owner, statistic_owner=None):
        '''
        For Impulse (and anything else that needs to delete forwards)
        '''
        starts, overall_finish = cls._missing_interval_start_dates(log, s, schedule, interval_owner, statistic_owner)
        if starts:
            return starts[0], time_to_local_date(overall_finish)
        else:
            return None, None

    @classmethod
    def missing_dates(cls, log, s, schedule, interval_owner, statistic_owner=None):
        '''
        Iterator over start DATES for all missing intervals, given the constraints supplied.
        '''
        starts, overall_finish = cls._missing_interval_start_dates(log, s, schedule, interval_owner, statistic_owner)
        for block_start in starts:
            yield from cls._missing_interval_dates_from(log, s, schedule, interval_owner, block_start, overall_finish)

    @classmethod
    def delete_all(cls, log, s):
        '''
        Efficiently delete all intervals.
        '''
        log.warning('Deleting all Intervals')
        # this uses the on delete cascade between source and interval
        s.query(Source).filter(Source.type == SourceType.INTERVAL).delete()

    @classmethod
    def clean_times(cls, s, start, finish, owner=None):
        '''
        Remove all intervals that include data in the given TIME range,
        '''
        cls.clean_dates(s, time_to_local_date(start), time_to_local_date(finish), owner=owner)

    @classmethod
    def clean_dates(cls, s, start, finish, owner=None):
        '''
        Remove all summary intervals (not monitor intervals) in the given DATE range.
        '''
        q = s.query(Interval).filter(Interval.start < finish, Interval.finish >= start)
        if owner:
            q = q.filter(Interval.owner == owner)
        for interval in q.all():
            s.delete(interval)
