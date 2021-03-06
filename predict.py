# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

"""Script to predict with a fully trained model.

Reads one source (TTL syntax) per line from stdin and writes one JSON line to
stdout.
"""

from collections import OrderedDict
import json
import logging
import sys
import utils
import time

import SPARQLWrapper
from splendid import chunker
from rdflib.util import from_n3


# noinspection PyUnresolvedReferences
import logging_config

# not all import on top due to scoop and init...

logger = logging.getLogger(__name__)


def _result_bundle(
        gps, source, gp_tcs,
        fusion_methods=None, max_results=0, max_target_candidates_per_gp=0,
):
    from fusion import fuse_prediction_results

    fused_results = fuse_prediction_results(
        gps,
        gp_tcs,
        fusion_methods
    )
    orig_length = max([len(v) for k, v in fused_results.items()])
    if max_results > 0:
        for k, v in fused_results.items():
            del v[max_results:]
    mt = max_target_candidates_per_gp
    if mt < 1:
        mt = None
    # logger.info(gp_tcs)
    res = {
        'source': source,
        'orig_result_length': orig_length,
        'graph_pattern_target_candidates': [sorted(tcs)[:mt] for tcs in gp_tcs],
        'fused_results': fused_results,
    }
    return res


def predict(
        sparql, timeout, gps, source,
        fusion_methods=None, max_results=0, max_target_candidates_per_gp=0,
):
    from gp_learner import predict_target_candidates
    gp_tcs = predict_target_candidates(sparql, timeout, gps, source)
    return _result_bundle(
        gps, source, gp_tcs,
        fusion_methods, max_results, max_target_candidates_per_gp
    )


def multi_predict(
        sparql, timeout, gps, sources,
        fusion_methods=None, max_results=0, max_target_candidates_per_gp=0,
):
    from gp_learner import predict_multi_target_candidates

    gp_stcs = predict_multi_target_candidates(sparql, timeout, gps, sources)
    res = []
    for source in sources:
        gp_tcs = [stcs[source] for stcs in gp_stcs]
        res.append(_result_bundle(
            gps, source, gp_tcs,
            fusion_methods, max_results, max_target_candidates_per_gp
        ))
    return res


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description='gp learner prediction',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--sparql_endpoint",
        help="the SPARQL endpoint to query",
        action="store",
        default=config.SPARQL_ENDPOINT,
    )
    parser.add_argument(
        "--max_queries",
        help="limits the amount of queries per prediction (0: no limit). "
             "You want to use the same limit as in training for late fusion "
             "models.",
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
        "--fusion_methods",
        help="Which fusion methods to use. During prediction, each of "
             "the learned patterns can generate a list of target candidates. "
             "Fusion re-combines these into a single ranked list of "
             "predicted targets. By default this will use all "
             "implemented fusion methods. Any of them, or a ',' delimited list "
             "can be used to reduce the output (just make sure you ran "
             "--predict=train_set on them before). Also supports 'basic' and "
             "'classifier' as shorthands. Make sure to only select methods the "
             "selected model was also trained on!",
        action="store",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--timeout",
        help="sets the timeout in seconds for each query (0: auto calibrate)",
        action="store",
        type=float,
        default=2.,
    )
    parser.add_argument(
        "--max_results",
        help="limits the result list lengths to save bandwidth (0: no limit)",
        action="store",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--max_target_candidates_per_gp",
        help="limits the target candidate list lengths to save bandwidth "
             "(0: no limit)",
        action="store",
        type=int,
        default=100,
    )

    parser.add_argument(
        "--batch_predict",
        help="will batch up to --BATCH_SIZE sources from stdin per query",
        action="store_true",
    )
    parser.add_argument(
        "--drop_bad_uris",
        help="URIs that cannot be curified are ignored",
        action="store_true",
    )

    parser.add_argument(
        "resdir",
        help="result directory of the trained model (overrides --RESDIR)",
        action="store",
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
        'RESDIR': prog_args['resdir'],
    })
    config.finalize(prog_args)

    return prog_args


def main(
        resdir,
        sparql_endpoint,
        max_queries,
        clustering_variant,
        fusion_methods,
        timeout,
        max_results,
        max_target_candidates_per_gp,
        batch_predict,
        drop_bad_uris,
        **_  # gulp remaining kwargs
):
    from gp_query import calibrate_query_timeout
    from serialization import load_results
    from serialization import find_last_result
    from cluster import cluster_gps_to_reduce_queries
    from gp_learner import init_workers

    # init workers
    init_workers()

    sparql = SPARQLWrapper.SPARQLWrapper(sparql_endpoint)
    timeout = timeout if timeout > 0 else calibrate_query_timeout(sparql)

    # load model
    last_res = find_last_result()
    if not last_res:
        logger.error('cannot find fully trained model in %s', resdir)
        sys.exit(1)
    result_patterns, coverage_counts, gtp_scores = load_results(last_res)
    gps = [gp for gp, _ in result_patterns]
    gps = cluster_gps_to_reduce_queries(
        gps, max_queries, gtp_scores, clustering_variant)

    processed = 0
    start = time.time()
    batch_size = config.BATCH_SIZE if batch_predict else 1
    # main loop
    for lines in chunker(sys.stdin, batch_size):
        batch = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if drop_bad_uris:
                # noinspection PyBroadException
                try:
                    source = from_n3(line)
                    utils.curify(source)
                except Exception:
                    logger.warning(
                        'Warning: Could not curify URI %s! Skip.', line)
                    continue
            if line[0] not in '<"':
                logger.error(
                    'expected inputs to start with < or ", but got: %s', line)
                sys.exit(1)
            source = from_n3(line)
            batch.append(source)
        batch = list(OrderedDict.fromkeys(batch))

        if len(batch) == 0:
            pass
        elif len(batch) == 1:
            res = predict(
                sparql, timeout, gps, batch[0], fusion_methods,
                max_results, max_target_candidates_per_gp
            )
            print(json.dumps(res))
            logger.info(
                'Predicted %d target candidates for %s',
                res['orig_result_length'], res['source']
            )
        else:
            res = multi_predict(
                sparql, timeout, gps, batch, fusion_methods,
                max_results, max_target_candidates_per_gp
            )
            for r in res:
                print(json.dumps(r))
            logger.info('\n'.join([
                'Predicted %d target candidates for %s' % (
                    r['orig_result_length'], r['source']
                ) for r in res
            ]))

        processed += len(batch)
        logger.info(
            'Have processed %d URIs now. Took %s sec',
            processed, time.time()-start)


if __name__ == "__main__":
    logger.info('init run: origin')
    import config
    prog_kwds = parse_args()
    main(**prog_kwds)
else:
    logger.info('init run: worker')
