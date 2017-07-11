# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os
import sys
from datetime import datetime
from functools import wraps
from os import path

import SPARQLWrapper
from rdflib import URIRef
from splendid import make_dirs_for
from splendid import timedelta_to_s

from flask import Flask
from flask import abort
from flask import jsonify
from flask import request
from flask_cors import CORS

# noinspection PyUnresolvedReferences
import logging_config

# not all import on top due to scoop and init...

logger = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app)


@app.route("/api/graph_patterns", methods=["GET"])
def graph_patterns():
    res = {
        'graph_patterns': GPS,
    }
    return jsonify(res)


@app.route("/api/predict", methods=["POST"])
def predict():
    from fusion import fuse_prediction_results
    from gp_learner import predict_target_candidates
    from gp_query import calibrate_query_timeout

    source = request.form.get('source')
    # logger.info(request.data)
    # logger.info(request.args)
    # logger.info(request.form)
    if not source:
        abort(400, 'no source given')
    source = URIRef(source)

    timeout = calibrate_query_timeout(SPARQL)
    gp_tcs = predict_target_candidates(SPARQL, timeout, GPS, source)
    fused_results = fuse_prediction_results(
        GPS,
        gp_tcs,
        FUSION_METHODS
    )
    res = {
        'source': source,
        'graph_patterns': GPS,
        # 'fused_results': {
        #     'target_occs': [
        #         ('http://dbpedia.org/resource/Dog', 42.24),
        #         ('http://dbpedia.org/resource/Cat', 42),
        #     ],
        # },
        'fused_results': fused_results
    }
    return jsonify(res)


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description='gp learner prediction model server',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # flask settings
    parser.add_argument(
        "--host",
        help="listen on IP",
        action="store",
        default="0.0.0.0",
    )
    parser.add_argument(
        "--port",
        help="port to listen on",
        action="store",
        default="8080",
    )
    parser.add_argument(
        "--flask_debug",
        help="flask debug mode",
        action="store_true",
        default=False,
    )

    # gp learner settings
    parser.add_argument(
        "--resdir",
        help="result directory of the model to serve (overrides --RESDIR)",
        action="store",
        required=True,
    )

    parser.add_argument(
        "--sparql_endpoint",
        help="the SPARQL endpoint to query",
        action="store",
        default=config.SPARQL_ENDPOINT,
    )

    parser.add_argument(
        "--associations_filename",
        help="ground truth source target file used for training and evaluation",
        action="store",
        default=config.GT_ASSOCIATIONS_FILENAME,
    )

    parser.add_argument(
        "--max_queries",
        help="limits the amount of queries per prediction (0: no limit)",
        action="store",
        type=int,
        default=100,
    )

    parser.add_argument(
        "--clustering_variant",
        help="if specified use this clustering variant for query reduction, "
             "otherwise select the best from various.",
        action="store",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--print_query_patterns",
        help="print the graph patterns which are used to make predictions",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--fusion_methods",
        help="Which fusion methods to train / use. During prediction, each of "
             "the learned patterns can generate a list of target candidates. "
             "Fusion allows to re-combine these into a single ranked list of "
             "predicted targets. By default this will train and use all "
             "implemented fusion methods. Any of them, or a ',' delimited list "
             "can be used to reduce the output (just make sure you ran "
             "--predict=train_set on them before). Also supports 'basic' and "
             "'classifier' as shorthands.",
        action="store",
        type=str,
        default=None,
    )

    cfg_group = parser.add_argument_group(
        'Advanced config overrides',
        'The following allow overriding default values from config/defaults.py'
    )
    config.arg_parse_config_vars(cfg_group)

    prog_args = vars(parser.parse_args())
    # the following were aliased above, make sure they're updated globally
    prog_args.update({
        'SPARQL_ENDPOINT': prog_args['sparql_endpoint'],
        'GT_ASSOCIATIONS_FILENAME': prog_args['associations_filename'],
        'RESDIR': prog_args['resdir'],
    })
    config.finalize(prog_args)

    return prog_args


def init(**kwds):
    from gp_learner import main
    return main(**kwds)


if __name__ == "__main__":
    logger.info('init run: origin')
    import config
    prog_kwds = parse_args()
    SPARQL, GPS, FUSION_METHODS = init(**prog_kwds)
    if prog_kwds['flask_debug']:
        logger.warning('flask debugging is active, do not use in production!')
    app.run(
        host=prog_kwds['host'],
        port=prog_kwds['port'],
        debug=prog_kwds['flask_debug'],
    )
else:
    logger.info('init run: worker')