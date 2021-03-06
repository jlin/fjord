import logging
import time
from itertools import islice

from django.conf import settings
from django.db import reset_queries

import requests
from elasticutils.contrib.django import get_es, MappingType
from elasticutils.contrib.django import S as BaseS
from elasticsearch.exceptions import ConnectionError, NotFoundError


# Note: This module should not import any Fjord modules. Otherwise we
# get into import recursion issues.


log = logging.getLogger('i.search')


_mapping_types = {}


def es_analyze(text, analyzer=None):
    """Returns analysis of text.

    :arg text: the text to analyze

    :arg analyzer: (optional) the analyzer to use. Defaults to snowball
        which is an English-settings analyzer.

    :returns: list of dicts each describing a token

    """
    es = get_es()
    index = get_index()
    analyzer = analyzer or 'snowball'

    ret = es.indices.analyze(index, body=text, analyzer=analyzer)

    return ret['tokens']


def register_mapping_type(mapping_type):
    """Registers a mapping type.

    This gives us a way to get all the registered mapping types for
    indexing.

    """
    _mapping_types[mapping_type.get_mapping_type_name()] = mapping_type
    # Enable this to be used as a decorator
    return mapping_type


def get_mapping_types(mapping_types=None):
    """Returns a dict of name -> mapping type.

    :arg mapping_types: list of mapping type names to restrict
        the dict to.

    """
    if mapping_types is None:
        return _mapping_types

    return dict((key, val) for key, val in _mapping_types.items()
                if key in mapping_types)


def get_index():
    """Returns the index we're using."""

    # Note: This could probably be defined in utils, but it's defined
    # here because otherwise models imports utils and utils imports
    # models and that turns into a mess.
    return '%s-%s' % (settings.ES_INDEX_PREFIX, settings.ES_INDEXES['default'])


class FjordS(BaseS):
    def process_query_sqs(self, key, val, action):
        """Implements simple_query_string query"""
        return {
            'simple_query_string': {
                'fields': [key],
                'query': val,
                'analyzer': 'snowball',
                'default_operator': 'or',
            }
        }


class FjordMappingType(MappingType):
    """DjangoMappingType with correct index."""
    @classmethod
    def get_index(cls):
        return get_index()

    @classmethod
    def search(cls):
        return FjordS(cls)


def boolean_type():
    return {'type': 'boolean'}


def date_type():
    return {'type': 'date'}


def integer_type():
    return {'type': 'integer'}


def keyword_type():
    return {'type': 'string', 'analyzer': 'keyword'}


def terms_type():
    return {'type': 'string', 'analyzer': 'standard'}


def text_type():
    return {'type': 'string', 'analyzer': 'snowball'}


def format_time(time_to_go):
    """Return minutes and seconds string for given time in seconds.

    :arg time_to_go: Number of seconds to go.

    :returns: string representation of how much time to go.
    """
    if time_to_go < 60:
        return '%ds' % time_to_go
    return '%dm %ds' % (time_to_go / 60, time_to_go % 60)


def create_batch_id():
    """Returns a batch_id"""
    # TODO: This is silly, but it's a good enough way to distinguish
    # between batches by looking at a Record. This is just over the
    # number of seconds in a day.
    return str(int(time.time()))[-6:]


def chunked(iterable, n):
    """Return chunks of n length of iterable.

    If ``len(iterable) % n != 0``, then the last chunk will have
    length less than n.

    Example:

    >>> chunked([1, 2, 3, 4, 5], 2)
    [(1, 2), (3, 4), (5,)]

    :arg iterable: the iterable
    :arg n: the chunk length

    :returns: generator of chunks from the iterable
    """
    iterable = iter(iterable)
    while 1:
        t = tuple(islice(iterable, n))
        if t:
            yield t
        else:
            return


def get_indexes(all_indexes=False):
    """Return list of (name, count) tuples for indexes.

    :arg all_indexes: True if you want to see all indexes and
        False if you want to see only indexes prefexed with
        ``settings.ES_INDEX_PREFIX``.

    :returns: list of (name, count) tuples.

    """
    status = get_es().indices.status()
    indexes = status['indices']

    if not all_indexes:
        indexes = dict((k, v) for k, v in indexes.items()
                       if k.startswith(settings.ES_INDEX_PREFIX))

    indexes = [(k, v['docs']['num_docs']) for k, v in indexes.items()]

    return indexes


def delete_index_if_exists(index):
    """Delete the specified index.

    :arg index: The name of the index to delete.

    """
    try:
        get_es().indices.delete(index)
    except NotFoundError:
        # Can ignore this since it indicates the index doesn't exist
        # and therefore there's nothing to delete.
        pass


def get_index_stats():
    """Return dict of name -> count for documents indexed.

    For example:

    >>> get_index_stats()
    {'response': 122233}

    .. Note::

       This infers the index to use from the registered mapping
       types.

    :returns: mapping type name -> count for documents indexes.

    :throws elasticsearch.exceptions.ConnectionError: if there's a
        connection error
    :throws elasticsearch.exceptions.NotFoundError: if the
        index doesn't exist

    """
    stats = {}
    for name, cls in get_mapping_types().items():
        stats[name] = FjordS(cls).count()

    return stats


def recreate_index(es=None):
    """Delete index if it's there and creates a new one.

    :arg es: ES to use. By default, this creates a new indexing ES.

    """
    if es is None:
        es = get_es()

    mappings = {}
    for name, mt in get_mapping_types().items():
        mapping = mt.get_mapping()
        if mapping is not None:
            mappings[name] = {'properties': mapping}

    index = get_index()

    delete_index_if_exists(index)

    # There should be no mapping-conflict race here since the index
    # doesn't exist. Live indexing should just fail.

    # Simultaneously create the index and the mappings, so live
    # indexing doesn't get a chance to index anything between the two
    # causing ES to infer a possibly bogus mapping (which causes ES to
    # freak out if the inferred mapping is incompatible with the
    # explicit mapping).

    es.indices.create(index, body={'mappings': mappings})


def get_indexable(percent=100, mapping_types=None):
    """Return list of (class, iterable) for all the things to index.

    :arg percent: Defaults to 100.  Allows you to specify how much of
        each doctype you want to index.  This is useful for
        development where doing a full reindex takes an hour.
    :arg mapping_types: List of mapping types to index. Defaults to
        indexing all mapping types.

    :returns: list of (mapping type class, iterable) for all mapping
        types

    """
    to_index = []
    percent = float(percent) / 100

    for name, cls in get_mapping_types(mapping_types).items():
        indexable = cls.get_indexable()
        if percent < 1:
            indexable = indexable[:int(indexable.count() * percent)]
        to_index.append((cls, indexable))

    return to_index


def index_chunk(cls, id_list, es=None):
    """Index a chunk of documents.

    :arg cls: The MappingType class.
    :arg id_list: Iterable of ids of that MappingType to index.
    :arg es: The ES to use. Defaults to creating a new indexing ES.

    """
    if es is None:
        es = get_es()

    for ids in chunked(id_list, 200):
        documents = []
        obj_list = cls.get_model().uncached.filter(id__in=ids)
        documents = [cls.extract_document(obj_id=obj.id, obj=obj)
                     for obj in obj_list]

        if documents:
            cls.bulk_index(documents, id_field='id', es=es)

    if settings.DEBUG:
        # Nix queries so that this doesn't become a complete
        # memory hog and make Will's computer sad when DEBUG=True.
        reset_queries()


def requires_good_connection(fun):
    """Decorator that logs an error on connection issues

    9 out of 10 doctors say that connection errors are usually because
    ES_URLS is set wrong. This catches those errors and helps you out
    with fixing it.

    """
    def _requires_good_connection(*args, **kwargs):
        try:
            return fun(*args, **kwargs)
        except ConnectionError:
            log.error('Either your ElasticSearch process is not quite '
                      'ready to rumble, is not running at all, or ES_URLS '
                      'is set wrong in your settings_local.py file.')
    return _requires_good_connection


@requires_good_connection
def es_reindex_cmd(percent=100, mapping_types=None):
    """Rebuild ElasticSearch indexes.

    :arg percent: 1 to 100--the percentage of the db to index
    :arg mapping_types: list of mapping types to index

    """
    es = get_es()

    log.info('Wiping and recreating %s....', get_index())
    recreate_index(es=es)

    # Shut off auto-refreshing.
    index_settings = es.indices.get_settings(index=get_index())
    old_refresh = (index_settings
                   .get(get_index(), {})
                   .get('settings', {})
                   .get('index.refresh_interval', '1s'))

    try:
        es.indices.put_settings(
            index=get_index(), body={'index': {'refresh_interval': '-1'}})

        if mapping_types:
            indexable = get_indexable(percent, mapping_types)
        else:
            indexable = get_indexable(percent)

        start_time = time.time()
        for cls, indexable in indexable:
            cls_start_time = time.time()
            total = len(indexable)

            if total == 0:
                continue

            log.info('Reindex %s. %s to index....',
                     cls.get_mapping_type_name(), total)

            i = 0
            for chunk in chunked(indexable, 1000):
                chunk_start_time = time.time()
                index_chunk(cls, chunk, es=es)

                i += len(chunk)
                time_to_go = (total - i) * ((time.time() - cls_start_time) / i)
                per_1000 = (time.time() - cls_start_time) / (i / 1000.0)
                this_1000 = time.time() - chunk_start_time

                log.info(
                    '   %s/%s %s... (%s/1000 avg, %s ETA)',
                    i,
                    total,
                    format_time(this_1000),
                    format_time(per_1000),
                    format_time(time_to_go)
                )

            delta_time = time.time() - cls_start_time
            log.info('   done! (%s total, %s/1000 avg)',
                     format_time(delta_time),
                     format_time(delta_time / (total / 1000.0)))

        delta_time = time.time() - start_time
        log.info('Done! (total time: %s)', format_time(delta_time))

    finally:
        es.indices.put_settings(
            index=get_index(),
            body={'index': {'refresh_interval': old_refresh}})


@requires_good_connection
def es_delete_cmd(index):
    """Delete a specified index."""
    indexes = [name for name, count in get_indexes()]

    if index not in indexes:
        log.error('Index "%s" is not a valid index.', index)
        if not indexes:
            log.error('There are no valid indexes.')
        else:
            log.error('Valid indexes: %s', ', '.join(indexes))
        return

    ret = raw_input('Are you sure you want to delete "%s"? (yes/no) ' % index)
    if ret != 'yes':
        return

    log.info('Deleting index "%s"...', index)
    delete_index_if_exists(index)
    log.info('Done!')


@requires_good_connection
def es_status_cmd(checkindex=False, log=log):
    """Show ElasticSearch index status."""
    log.info('Settings:')
    log.info('  ES_URLS               : %s', settings.ES_URLS)
    log.info('  ES_INDEX_PREFIX       : %s', settings.ES_INDEX_PREFIX)
    log.info('  ES_INDEXES            : %s', settings.ES_INDEXES)

    # FIXME - can do this better with elasticsearch API.
    try:
        es_deets = requests.get(settings.ES_URLS[0]).json()
        log.info('  Elasticsearch version : %s', es_deets['version']['number'])
    except requests.exceptions.RequestException:
        log.info('  Could not connect to Elasticsearch')

    log.info('Index (%s) stats:', get_index())

    try:
        mt_stats = get_index_stats()
        log.info('  Index (%s):', get_index())
        for name, count in mt_stats.items():
            log.info('    %-20s: %d', name, count)

    except NotFoundError:
        log.info('  Index does not exist. (%s)', get_index())
