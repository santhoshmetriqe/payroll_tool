"""DB config/connection helpers shared by the v2 ingestor pipeline."""
import configparser
import os
import sys

import psycopg2


def load_config(path):
    """Read config.ini -> dict with 'database' kwargs and 'defaults'."""
    cfg = configparser.ConfigParser()
    if not cfg.read(path, encoding='utf-8'):
        return {}
    out = {}
    if cfg.has_section('database'):
        out['database'] = dict(cfg.items('database'))
    if cfg.has_section('defaults'):
        out['defaults'] = {k: v for k, v in cfg.items('defaults') if v}
    return out


def connect_db(args, cfg):
    """Open a DB connection from --dsn, else PG_DSN env, else config.ini."""
    if args.dsn:
        return psycopg2.connect(args.dsn)
    if os.environ.get('PG_DSN'):
        return psycopg2.connect(os.environ['PG_DSN'])
    if cfg.get('database'):
        return psycopg2.connect(**cfg['database'])
    sys.exit('No DB credentials: provide --dsn, PG_DSN, or a [database] section in config.ini')


def tbl(prop, name):
    return f'"{prop}_{name}"'
