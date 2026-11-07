"""Isolated low-concurrency news runner; no video worker slots are consumed."""
import argparse,json,time,signal
from factory.db import SessionLocal
from factory.services.xnews_runtime import tick

class NewsTickDeadline(BaseException):
    pass

def _deadline(signum,frame):
    raise NewsTickDeadline()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    signal.signal(signal.SIGALRM,_deadline)
    while True:
        signal.alarm(230)
        try:print(json.dumps(tick(SessionLocal),ensure_ascii=False),flush=True)
        except NewsTickDeadline:print(json.dumps({'status':'time_budget_exhausted','message':'İşin kalıcı kaydı korunarak tur sonlandırıldı.'}),flush=True)
        except Exception as exc:print(json.dumps({'status':'error','error_type':type(exc).__name__}),flush=True)
        finally:signal.alarm(0)
        if args.once:break
        time.sleep(30)
if __name__=='__main__':main()
