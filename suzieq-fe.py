#!/usr/bin/python3

import sys
import os
import json
import logging
import argparse

from multiprocessing import Process, Manager
from threading import Thread
from time import sleep

import inotify.adapters

from livylib import get_or_create_livysession, exec_livycode

initcode = """
import json
import os
import logging

from pyspark.sql.types import StructType, StructField, StringType

def refresh_tables(datadir, schema_dir, tmpdir):
    '''Build a view containing only the latest data associated with all tables'''

    schemas = get_schemas(schema_dir)

    for root, dirs, files in os.walk(datadir):
        for topic in dirs:
            if topic in schemas:
                refresh_single_table(topic, (datadir + '/' + topic),
                                     schemas[topic], tmpdir)
        break

def get_schemas(schema_dir):

    schemas = {}

    if not os.path.exists(schema_dir):
        logging.error('Schema directory does not exist')
        return schemas

    for root, dirs, files in os.walk(schema_dir):
        for topic in files:
            with open(root + '/' + topic, 'r') as f:
                data = json.loads(f.read())
                schemas[data['name']] = data['fields']
        break

    return schemas

try:
    spark
except NameError:
    print('Spark session not found')

refresh_tables('%s', '%s', '%s')
"""

refresh_table_code = """
import sys
import os

def refresh_single_table(topic, datadir, sch, tmpdir):
    '''Refresh the table for a single topic'''

    if not topic or not os.path.isdir(datadir):
        return

    keys = [x['name'] for x in sch if x.get('key', False)]
    fields = ['last(' + x['name'] + ') as ' + x['name']
              for x in sch if (not x.get('key', False))]

    adf = spark \
          .read \
          .option('mergeSchema', True) \
          .parquet(datadir) \
          .orderBy('timestamp')

    adf.createOrReplaceTempView('a_' + topic)

    selstr = 'select {fields}, {keys} from a_{topic} ' \
             'group by {keys}'.format(topic=topic, keys=', '.join(keys),
                                      fields=', '.join(fields))

    tdf = spark.sql(selstr) \
               .filter(col('active') == '1') \
               .drop(col('active')) \
               .write \
               .saveAsTable(topic, format='parquet', mode='overwrite',
                            path='{dir}/{topic}'.format(dir=tmpdir, topic=topic))


"""


def inotify_process(state, datadir, notify_refresh):

    i = inotify.adapters.Inotify()
    watch_events = (
        inotify.constants.IN_CREATE | inotify.constants.IN_DELETE |
        inotify.constants.IN_MODIFY | inotify.constants.IN_DELETE_SELF
        )

    for root, dirs, files in os.walk(datadir):
        for topic in dirs:
            i.add_watch('{0}/{1}'.format(root, topic), watch_events)
        break

    while True:
        for event in i.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event
            if 'IN_DELETE_SELF' in type_names:
                # This path got deleted, remove it and add it back
                i.add_watch(path, watch_events)
                continue

            state[os.path.basename(path)] = True
            if state['update'] is False:
                state['update'] = True
                with notify_refresh:
                    notify_refresh.notify()


def background_refresh_tables(state, datadir, schemas, notify_refresh,
                              session_url, tmpdir):

    call_func = '\nrefresh_single_table("{0}", "{1}", "{2}", "{3}")'
    print('Background refresh process started')
    while True:
        try:
            with notify_refresh:
                notify_refresh.wait()
        except IOError:
            # Handle this to avoid traceback on exit
            print('Background update exiting on shutdown')
            return

    for root, dirs, files in os.walk(datadir):
        for topic in dirs:
            if state.get(topic, False) and topic in schemas:
                code_exp = call_func.format(topic, datadir,
                                            schemas[topic],
                                            tmpdir)
                _ = exec_livycode(refresh_table_code + code_exp,
                                  session_url, True)
                state[topic] = False
            state['update'] = False
        break


def get_schemas(schema_dir):

    schemas = {}

    if not os.path.exists(schema_dir):
        logging.error('Schema directory {} does not exist'.format(schema_dir))
        return schemas

    for root, dirs, files in os.walk(schema_dir):
        for topic in files:
            with open(root + '/' + topic, 'r') as f:
                data = json.loads(f.read())
                schemas[data['name']] = data['fields']
        break

    return schemas


def _main(datadir, schema_dir, tmpdir):

    schemas = get_schemas(schema_dir)
    if not schemas:
        print('No schemas found. Aborting')
        sys.exit(1)

    manager = Manager()
    state = manager.dict({'update': False})
    for root, dirs, files in os.walk(schema_dir):
        for f in files:
            topic = os.path.splitext(f)[0]
            if topic in schemas:
                manager.dict({topic: False})
        break

    notify_refresh = manager.Condition()

    # Need this for jupyter notebook
    import warnings
    warnings.filterwarnings("ignore", message="numpy.dtype size changed")
    warnings.filterwarnings("ignore", message="numpy.ufunc size changed")

    notify_proc = Process(target=inotify_process,
                          args=(state, datadir, notify_refresh))
    notify_proc.daemon = True
    notify_proc.start()

    session_url, response = get_or_create_livysession()
    if not session_url:
        print('Unable to create a Livy session. Aborting')
        sys.exit(1)

    output = exec_livycode(refresh_table_code + initcode % (datadir,
                                                            schema_dir,
                                                            tmpdir),
                           session_url, True)
    if output['status'] != 'ok':
        print(output)
    else:
        print('All tables created')

    update_proc = Thread(target=background_refresh_tables,
                         args=(state, datadir, schemas, notify_refresh,
                               session_url, tmpdir))
    update_proc.daemon = True
    update_proc.start()

    return session_url


if __name__ == '__main__':

    parser = argparse.ArgumentParser('suzieq-fe')
    parser.add_argument('-D', '--data-dir', type=str, required=True,
                        help='Location of the data files')
    parser.add_argument('-T', '--schema-dir', type=str, required=True,
                        help='Directory where schema files are located')
    parser.add_argument('-O', '--temp-dir', type=str, required=True,
                        help='Location to store temporary files')
    parser.add_argument('-l', '--log', type=str, default='WARNING',
                        choices=['ERROR', 'WARNING', 'INFO', 'DEBUG'],
                        help='Logging message level, default is WARNING')

    args = parser.parse_args()

    logging.basicConfig(filename='/tmp/suzieq-fe.log',
                        level=getattr(logging, args.log.upper()),
                        format='%(asctime)s - %(name)s - %(levelname)s'
                        '- %(message)s')

    logger = logging.getLogger('suzieq-fe')

    session_url = _main(args.data_dir, os.path.abspath(args.schema_dir),
                        args.temp_dir)

    while True:
        # Sleep and send keepalives to keep the Spark session alive
        output = exec_livycode("""spark.conf.get('spark.app.name')""",
                               session_url)
        if output['status'] != 'ok':
            print(output)
        sleep(180)
    