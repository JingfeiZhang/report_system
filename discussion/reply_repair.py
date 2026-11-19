"""Repair missing public replies individually; only publish a complete revision."""
import copy
import json
import re
import time

from . import checkpoints


def repair_replies(service, state, task, instruction, data, capture):
    from .debate import IncompleteExchange, reply_validation
    from .service import error_text

    previous = data['previous']
    spec = previous['reply_repair']
    required = set(spec['required_challenge_ids'])
    allowed = {e['id'] for e in data['evidence']}
    identity = service.provider.call_identity(task['role']) if hasattr(service.provider, 'call_identity') else type(service.provider).__name__
    fingerprint = checkpoints.digest(['individual-replies-v1', task, previous, data['evidence'], data['sentiment'], identity])
    folder = service.round_folder(state)
    path = folder / 'repairs' / (task['id'] + '.json')
    saved = checkpoints.read(path)
    replies = saved['replies'] if saved and saved.get('fingerprint') == fingerprint else []
    _, replies = reply_validation(replies, required)
    replies = [r for r in replies if set(re.findall(r'E-[a-f0-9]{12}', json.dumps(r))) <= allowed]
    errors = []
    capture['requests'] = 0

    def progress():
        completed = {r['challenge_id'] for r in replies}
        pending = required - completed
        questions = [dict(id=c['id'], question=c['question'], resolution=c.get('resolution', ''))
                     for review in previous['peer_reviews'] for c in review['challenges'] if c['id'] in pending]
        info = dict(total=len(required), completed=len(completed), pending=questions, errors=list(errors))
        def update(s):
            item = next(t for t in s['rounds'][-1]['tasks'] if t['id'] == task['id'])
            item['reply_repair'] = info
        service._change(state['id'], state['operation'], update, 'task_status')
        checkpoints.write(path, dict(fingerprint=fingerprint, replies=replies))
        return pending

    pending = progress()
    # Empty requirements can still need an explicit correction of unknown extra IDs.
    for cid in sorted(pending) if required else [None]:
        current = service.store.get(state['id'], summary=True)
        if current.get('operation') != state['operation'] or current['rounds'][-1].get('stop_requested'):
            raise RuntimeError('用户已终止本轮，未继续发起补答请求')
        request = copy.deepcopy(data)
        request['previous']['reply_repair']['required_challenge_ids'] = [cid] if cid else []
        request['previous']['reply_repair']['preserved_replies'] = spec['preserved_replies'] + replies
        request['previous']['peer_reviews'] = [dict(r, challenges=[c for c in r['challenges'] if c['id'] == cid])
                                               for r in previous['peer_reviews'] if any(c['id'] == cid for c in r['challenges'])]
        request['reply_requirements'] = [dict(challenge_id=c['id'], question=c['question'], resolution=c.get('resolution', ''))
                                       for r in request['previous']['peer_reviews'] for c in r['challenges']]
        result = None
        try:
            capture['requests'] += 1
            result = service.provider.json(task['role'], instruction, request)
            validation, valid = reply_validation(result.get('replies'), {cid} if cid else set())
            if validation['errors']:
                raise IncompleteExchange('单项补答未通过校验', validation)
            if not set(re.findall(r'E-[a-f0-9]{12}', json.dumps(result))) <= allowed:
                raise RuntimeError('补答包含未知引用，结果未采纳')
            replies.extend(valid)
        except Exception as exc:
            message = error_text(exc)
            errors.append(dict(challenge_id=cid, message=message,
                               error_type='response_validation' if isinstance(exc, IncompleteExchange) else
                               'timeout' if 'Timeout' in message else 'execution_error'))
            checkpoints.write(folder / 'failures' / f"{task['id']}-reply-{time.time_ns()}.json",
                              dict(task_id=task['id'], challenge_id=cid, model_output=result,
                                   validation=getattr(exc, 'validation', {'errors': [message]})))
        pending = progress()
    if pending or (not required and errors):
        if errors and all(e['error_type'] != 'response_validation' for e in errors):
            raise RuntimeError('修订补答请求未完成：' + '；'.join(e['message'] for e in errors))
        raise IncompleteExchange('修订仍有待补回应：' + '；'.join(e['message'] for e in errors),
                                 dict(missing_challenge_ids=sorted(pending), errors=[e['message'] for e in errors]))
    return dict(previous['revision_draft'], replies=spec['preserved_replies'] + replies)
