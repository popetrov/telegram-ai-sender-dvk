"""Recover only untouched 2.0.45 infrastructure handoffs, then verify live history."""
import json
from finance_scope import thread_allowed
from dialogue_seller import event

SUMMARY='Ответ остановлен: не удалось безопасно завершить обработку. Проверьте диалог. Тип: OperationalError'

def recover_calendar_failure(store,thread_id):
 with store.connection() as db:
  db.execute('BEGIN IMMEDIATE')
  t=store._thread(db,thread_id)
  if (not thread_allowed(t) or not t['seller_enabled'] or t['state']!='human'
      or t['mode']!='copilot' or not t['outgoing_allowed'] or not t['history_complete']
      or t['control_reason']!=SUMMARY):return False
  n=db.execute('SELECT * FROM dialogue_notices WHERE thread_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(thread_id,)).fetchone()
  e=db.execute('SELECT * FROM dialogue_control_events WHERE thread_id=? ORDER BY rowid DESC LIMIT 1',(thread_id,)).fetchone()
  if (not n or n['event_type']!='runtime' or n['summary']!=SUMMARY
      or not n['event_key'].startswith('seller_error:')
      or n['status'] not in {'pending','delivered'} or not e
      or e['event']!='handoff_paused' or n['event_key']!='seller_error:'+str(e['revision']-1)
      or json.loads(e['data']).get('event_type')!='runtime'):return False
  if db.execute("SELECT 1 FROM dialogue_actions WHERE thread_id=? AND status IN ('sending','uncertain')",(thread_id,)).fetchone():return False
  # No direct send or replay. Worker must reread Telegram before finish_resume.
  now=int(store.clock())
  db.execute("UPDATE dialogue_threads SET mode='active_negotiation',resume_pending=1,control_epoch=control_epoch+1,revision=revision+1,control_reason=?,updated_at=? WHERE id=?",
   ('Восстановление после ошибки календаря 2.0.45. Проверяется свежая переписка.',now,thread_id))
  db.execute("UPDATE dialogue_notices SET status='handled' WHERE id=?",(n['id'],))
  # Restore only untouched, never-queued calendar rows cancelled by this exact handoff.
  # Existing action identities (including uncertain/cancelled sends) are never replayed.
  restored=db.execute("UPDATE seller_followups SET status='scheduled',reason='Восстановлено после ошибки календаря 2.0.45',revision=revision+1,updated_at=? WHERE thread_id=? AND status='cancelled' AND reason='Передача МОП: runtime' AND updated_at=? AND action_id IS NULL",
   (now,thread_id,n['created_at'])).rowcount
  if restored:
   db.execute("UPDATE seller_warm_cohorts SET status='active',revision=revision+1,updated_at=? WHERE thread_id=? AND status='paused'",(now,thread_id))
  event(db,thread_id,'calendar_failure_recovery',t['revision']+1,now,notice_id=n['id'])
  return True
