import atexit
import traceback
import sys
import os
import time
import asyncio
import datetime
import platform

import torch.multiprocessing as mp

from mindsdb.utilities.config import Config
from mindsdb.utilities.os_specific import get_mp_context
from mindsdb.interfaces.native.native import NativeInterface
from mindsdb.interfaces.custom.custom_models import CustomModels
from mindsdb.api.http.start import start as start_http
from mindsdb.api.mysql.start import start as start_mysql
from mindsdb.api.mongo.start import start as start_mongo
from mindsdb.utilities.ps import is_pid_listen_port
from mindsdb.interfaces.database.database import DatabaseWrapper
from mindsdb.utilities.functions import args_parse, get_all_models_meta_data
from mindsdb.utilities.log import log


def close_api_gracefully(apis):
    try:
        for api in apis.values():
            process = api['process']
            sys.stdout.flush()
            process.terminate()
            process.join()
            sys.stdout.flush()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    mp.freeze_support()
    args = args_parse()
    config = Config()

    if args.verbose is True:
        config.set(['log', 'level', 'console'], 'DEBUG')

    os.environ['DEFAULT_LOG_LEVEL'] = config['log']['level']['console']
    os.environ['LIGHTWOOD_LOG_LEVEL'] = config['log']['level']['console']
    config.set(['mindsdb_last_started_at'], str(datetime.datetime.now()))
    
    from lightwood.__about__ import __version__ as lightwood_version
    from mindsdb_native.__about__ import __version__ as mindsdb_native_version
    from mindsdb.__about__ import __version__ as mindsdb_version
    print('Versions:')
    print(f' - lightwood {lightwood_version}')
    print(f' - MindsDB_native {mindsdb_native_version}')
    print(f' - MindsDB {mindsdb_version}')

    print(f'Configuration file:\n   {config.config_path}')
    print(f"Storage path:\n   {config.paths['root']}")

    if args.api is None:
        api_arr = ['http', 'mysql']
    else:
        api_arr = args.api.split(',')

    apis = {
        api: {
            'port': config['api'][api]['port'],
            'process': None,
            'started': False
        } for api in api_arr
    }

    for api_name in apis.keys():
        if api_name not in config['api']:
            print(f"Trying run '{api_name}' API, but is no config for this api.")
            print(f"Please, fill config['api']['{api_name}']")
            sys.exit(0)

    start_functions = {
        'http': start_http,
        'mysql': start_mysql,
        'mongodb': start_mongo
    }

    mdb = NativeInterface()
    cst = CustomModels()

    model_data_arr = get_all_models_meta_data(mdb, cst)

    dbw = DatabaseWrapper()
    for db_alias in config['integrations']:
        dbw.setup_integration(db_alias)
    dbw.register_predictors(model_data_arr)

    for broken_name in [name for name, connected in dbw.check_connections().items() if connected is False]:
        log.error(f'Error failed to integrate with database aliased: {broken_name}')

    ctx = mp.get_context('spawn')
    # Switch to this once the native interface has it's own thread :/
    # ctx = mp.get_context(get_mp_context())


    for api_name, api_data in apis.items():
        print(f'{api_name} API: starting...')
        try:
            if api_name == 'http':
                p = ctx.Process(target=start_functions[api_name], args=(args.verbose,args.no_studio))
            else:
                p = ctx.Process(target=start_functions[api_name], args=(args.verbose,))
            p.start()
            api_data['process'] = p
        except Exception as e:
            log.error(f'Failed to start {api_name} API with exception {e}\n{traceback.format_exc()}')
            close_api_gracefully(apis)
            raise e

    atexit.register(close_api_gracefully, apis=apis)

    async def wait_api_start(api_name, pid, port):
        timeout = 60
        start_time = time.time()
        started = is_pid_listen_port(pid, port)
        while (time.time() - start_time) < timeout and started is False:
            await asyncio.sleep(0.5)
            started = is_pid_listen_port(pid, port)
        return api_name, port, started

    async def wait_apis_start():
        futures = [
            wait_api_start(api_name, api_data['process'].pid, api_data['port'])
            for api_name, api_data in apis.items()
        ]
        for i, future in enumerate(asyncio.as_completed(futures)):
            api_name, port, started = await future
            if started:
                print(f"{api_name} API: started on {port}")
            else:
                log.error(f"ERROR: {api_name} API cant start on {port}")

    ioloop = asyncio.get_event_loop()
    ioloop.run_until_complete(wait_apis_start())
    ioloop.close()

    try:
        for api_data in apis.values():
            api_data['process'].join()
    except KeyboardInterrupt:
        print('Closing app...')
