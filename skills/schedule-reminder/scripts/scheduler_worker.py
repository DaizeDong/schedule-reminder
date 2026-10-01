"""Selected scheduler entrypoint; successful receipt evidence is PRIVATE DATA."""
import argparse
import json
import os
import sys

import capabilities
import private_data


def run(capability, db_path, config):
    private_data.prove_private(db_path)
    private_data.prove_private(config)
    os.environ['SCHEDULE_DB_PATH'] = db_path
    os.environ['AGENT_CENTER_CONFIG'] = config
    if capability == 'remind':
        import store
        result = store.tick(db_path=db_path)
        if not result['retried'] and not result['blocked'] and result.get('delivery_receipts'):
            capabilities.publish_delivery('remind', result['delivery_receipts'][-1], db_path, config, python=sys.executable)
        result['status'] = 'partial' if result['retried'] or result['blocked'] else 'completed'
        if result['status'] == 'partial':
            result.update(
                error_code='ERR_DELIVERY_FAILED',
                message='Reminder delivery failed for one or more items.',
                action=('Check the reminder relay configuration and delivery errors. '
                        'Retried items will retry after their next_retry_at; '
                        'review blocked items and re-arm them after fixing delivery.'),
            )
        return result
    if capability == 'ingest':
        import ingest_tick
        result = ingest_tick.run()
    elif capability == 'work':
        import agent_tick
        result = agent_tick.run()
    else:
        raise ValueError('unsupported scheduled capability')
    return {'status': 'unmeasured', 'result': result,
            'reason': 'this worker has not supplied its own confirmed delivery receipt'}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--capability', choices=tuple(capabilities.TASKS), required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.capability, args.db, args.config)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}))
        return 1
    print(json.dumps(result))
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
