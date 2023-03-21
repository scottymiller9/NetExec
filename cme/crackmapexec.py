#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from cme.logger import setup_logger, setup_debug_logger, CMEAdapter
from cme.helpers.logger import highlight
from cme.helpers.misc import identify_target_file
from cme.parsers.ip import parse_targets
from cme.parsers.nmap import parse_nmap_xml
from cme.parsers.nessus import parse_nessus_file
from cme.cli import gen_cli_args
from cme.loaders.protocol_loader import protocol_loader
from cme.loaders.module_loader import module_loader
from cme.servers.http import CMEServer
from cme.first_run import first_run_setup
from cme.context import Context
from cme.paths import CME_PATH
from concurrent.futures import ThreadPoolExecutor
from pprint import pformat
from decimal import Decimal
import asyncio
import aioconsole
import functools
import configparser
import cme.helpers.powershell as powershell
import cme
import shutil
import webbrowser
import sqlite3
import random
import os
import sys
import logging
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.exc import SAWarning
import warnings

Base = declarative_base()

setup_logger()
logger = CMEAdapter()

try:
    import librlers
except:
    print("Incompatible python version, try with another python version or another binary 3.8 / 3.9 / 3.10 / 3.11 that match your python version (python -V)")
    sys.exit()
# if there is an issue with SQLAlchemy and a connection cannot be cleaned up properly it spews out annoying warnings
warnings.filterwarnings("ignore", category=SAWarning)


def create_db_engine(db_path):
    db_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        isolation_level="AUTOCOMMIT",
        future=True
    )  # can add echo=True
    # db_engine.execution_options(isolation_level="AUTOCOMMIT")
    # db_engine.connect().connection.text_factory = str
    return db_engine


async def monitor_threadpool(pool, targets):
    logging.debug('Started thread poller')

    while True:
        try:
            text = await aioconsole.ainput("")
            if text == "":
                pool_size = pool._work_queue.qsize()
                finished_threads = len(targets) - pool_size
                percentage = Decimal(finished_threads) / Decimal(len(targets)) * Decimal(100)
                logger.info(f"completed: {percentage:.2f}% ({finished_threads}/{len(targets)})")
        except asyncio.CancelledError:
            logging.debug("Stopped thread poller")
            break


async def run_protocol(loop, protocol_obj, args, db, target, jitter):
    try:
        if jitter:
            value = random.choice(range(jitter[0], jitter[1]))
            logging.debug(f"Doin' the jitterbug for {value} second(s)")
            await asyncio.sleep(value)

        thread = loop.run_in_executor(
            None,
            functools.partial(
                protocol_obj,
                args,
                db,
                str(target)
            )
        )

        await asyncio.wait_for(
            thread,
            timeout=args.timeout
        )

    except asyncio.TimeoutError:
        logging.debug("Thread exceeded timeout")
    except asyncio.CancelledError:
        logging.debug("Shutting down DB")
        thread.cancel()
    except sqlite3.OperationalError as e:
        logging.debug("Sqlite error - sqlite3.operationalError - {}".format(str(e)))


async def start_threadpool(protocol_obj, args, db, targets, jitter):
    pool = ThreadPoolExecutor(max_workers=args.threads + 1)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(pool)

    monitor_task = asyncio.create_task(
        monitor_threadpool(pool, targets)
    )

    jobs = [
        run_protocol(
            loop,
            protocol_obj,
            args,
            db,
            target,
            jitter
        )
        for target in targets
    ]

    try:
        logging.debug("Running")
        await asyncio.gather(*jobs)
    except asyncio.CancelledError:
        print('\n')
        logger.info("Shutting down, please wait...")
        logging.debug("Cancelling scan")
    finally:
        await asyncio.shield(db.shutdown_db())
        monitor_task.cancel()
        pool.shutdown(wait=True)


def main():
    logging.getLogger('asyncio').setLevel(logging.CRITICAL)
    logging.getLogger('aiosqlite').setLevel(logging.CRITICAL)
    logging.getLogger('pypsrp').setLevel(logging.CRITICAL)
    logging.getLogger('spnego').setLevel(logging.CRITICAL)
    logging.getLogger('sqlalchemy.pool.impl.NullPool').setLevel(logging.CRITICAL)
    first_run_setup(logger)

    args = gen_cli_args()

    if args.darrell:
        links = open(os.path.join(os.path.dirname(cme.__file__), 'data', 'videos_for_darrell.harambe')).read().splitlines()
        try:
            webbrowser.open(random.choice(links))
        except:
            sys.exit(1)

    config = configparser.ConfigParser()
    config.read(os.path.join(CME_PATH, 'cme.conf'))

    module = None
    module_server = None
    targets = []
    jitter = None
    server_port_dict = {'http': 80, 'https': 443, 'smb': 445}
    current_workspace = config.get('CME', 'workspace')
    if config.get('CME', 'log_mode') != "False":
        logger.setup_logfile()

    if args.verbose:
        setup_debug_logger()

    logging.debug('Passed args:\n' + pformat(vars(args)))

    if args.jitter:
        if '-' in args.jitter:
            start, end = args.jitter.split('-')
            jitter = (int(start), int(end))
        else:
            jitter = (0, int(args.jitter))

    if hasattr(args, 'cred_id') and args.cred_id:
        for cred_id in args.cred_id:
            if '-' in str(cred_id):
                start_id, end_id = cred_id.split('-')
                try:
                    for n in range(int(start_id), int(end_id) + 1):
                        args.cred_id.append(n)
                    args.cred_id.remove(cred_id)
                except Exception as e:
                    logger.error('Error parsing database credential id: {}'.format(e))
                    sys.exit(1)

    if hasattr(args, 'target') and args.target:
        for target in args.target:
            if os.path.exists(target):
                target_file_type = identify_target_file(target)
                if target_file_type == 'nmap':
                    targets.extend(parse_nmap_xml(target, args.protocol))
                elif target_file_type == 'nessus':
                    targets.extend(parse_nessus_file(target, args.protocol))
                else:
                    with open(target, 'r') as target_file:
                        for target_entry in target_file:
                            targets.extend(parse_targets(target_entry.strip()))
            else:
                targets.extend(parse_targets(target))

    # The following is a quick hack for the powershell obfuscation functionality, I know this is yucky
    if hasattr(args, 'clear_obfscripts') and args.clear_obfscripts:
        shutil.rmtree(os.path.expanduser('~/.cme/obfuscated_scripts/'))
        os.mkdir(os.path.expanduser('~/.cme/obfuscated_scripts/'))
        logger.success('Cleared cached obfuscated PowerShell scripts')

    if hasattr(args, 'obfs') and args.obfs:
        powershell.obfuscate_ps_scripts = True

    logging.debug(f"Protocol: {args.protocol}")
    p_loader = protocol_loader()
    protocol_path = p_loader.get_protocols()[args.protocol]['path']
    logging.debug(f"Protocol Path: {protocol_path}")
    protocol_db_path = p_loader.get_protocols()[args.protocol]['dbpath']
    logging.debug(f"Protocol DB Path: {protocol_db_path}")

    protocol_object = getattr(p_loader.load_protocol(protocol_path), args.protocol)
    logging.debug(f"Protocol Object: {protocol_object}")
    protocol_db_object = getattr(p_loader.load_protocol(protocol_db_path), 'database')
    logging.debug(f"Protocol DB Object: {protocol_db_object}")

    db_path = os.path.join(CME_PATH, 'workspaces', current_workspace, args.protocol + '.db')
    logging.debug(f"DB Path: {db_path}")

    db_engine = create_db_engine(db_path)

    db = protocol_db_object(db_engine)

    setattr(protocol_object, 'config', config)

    if hasattr(args, 'module'):
        loader = module_loader(args, db, logger)
        if args.list_modules:
            modules = loader.get_modules()

            for name, props in sorted(modules.items()):
                logger.info('{:<25} {}'.format(name, props['description']))
            sys.exit(0)

        elif args.module and args.show_module_options:

            modules = loader.get_modules()
            for name, props in modules.items():
                if args.module.lower() == name.lower():
                    logger.info('{} module options:\n{}'.format(name, props['options']))
            sys.exit(0)

        elif args.module:
            modules = loader.get_modules()
            for name, props in modules.items():
                if args.module.lower() == name.lower():
                    module = loader.init_module(props['path'])
                    setattr(protocol_object, 'module', module)
                    break

            if not module:
                logger.error('Module not found')
                exit(1)

            if getattr(module, 'opsec_safe') is False:
                ans = input(highlight('[!] Module is not opsec safe, are you sure you want to run this? [Y/n] ', 'red'))
                if ans.lower() not in ['y', 'yes', '']:
                    sys.exit(1)

            if getattr(module, 'multiple_hosts') is False and len(targets) > 1:
                ans = input(highlight("[!] Running this module on multiple hosts doesn't really make any sense, are you sure you want to continue? [Y/n] ", 'red'))
                if ans.lower() not in ['y', 'yes', '']:
                    sys.exit(1)

            if hasattr(module, 'on_request') or hasattr(module, 'has_response'):

                if hasattr(module, 'required_server'):
                    args.server = getattr(module, 'required_server')

                if not args.server_port:
                    args.server_port = server_port_dict[args.server]

                context = Context(db, logger, args)
                module_server = CMEServer(module, context, logger, args.server_host, args.server_port, args.server)
                module_server.start()
                setattr(protocol_object, 'server', module_server.server)

    if (args.ntds and not args.userntds):
        ans = input(highlight('[!] Dumping the ntds can crash the DC on Windows Server 2019. Use the option --user <user> to dump a specific user safely [Y/n] ', 'red'))
        if ans.lower() not in ['y', 'yes', '']:
            sys.exit(1)

    try:
        asyncio.run(
            start_threadpool(protocol_object, args, db, targets, jitter)
        )
    except KeyboardInterrupt:
        logging.debug("Got keyboard interrupt")
    finally:
        if module_server:
            module_server.shutdown()
        asyncio.run(db_engine.dispose())


if __name__ == '__main__':
    main()
