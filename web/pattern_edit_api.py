"""Authenticated HTTP adapters for the shared native/web editor service."""
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core.pattern_edit_store import EditError, Conflict
from core.pattern_edit_service import get_pattern_edit_service
from core.paper_books import paper_books
from web.auth import require_login


def install_pattern_edit_routes(app):
    async def body(request):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403,'Cross-origin editor mutations are not allowed')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403,'Cross-site editor mutations are not allowed')
        raw = await request.body()
        if len(raw)>13*1024*1024:
            raise HTTPException(413,'Editor request exceeds limit')
        import json
        try:
            value = json.loads(raw) if raw else {}
            if not isinstance(value,dict):
                raise ValueError()
            return value
        except ValueError:
            raise HTTPException(400,'Expected JSON object') from None

    async def call(method,*args,**kwargs):
        try:
            return await run_in_threadpool(method,*args,**kwargs)
        except Conflict as exc:
            raise HTTPException(409,str(exc)) from None
        except EditError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.post('/api/pattern-edits')
    async def create(request:Request,user=Depends(require_login)):
        data = await body(request)
        if data.get('market') not in ('us','ph') or not data.get('trade_id'):
            raise HTTPException(422,'Select a paper market and stable trade identity')
        chart = await run_in_threadpool(paper_books.chart,data['market'],side='open',trade_id=data['trade_id'])
        if chart.get('error'):
            raise HTTPException(404,chart['error'])
        return await call(get_pattern_edit_service().create,chart,user)

    @app.get('/api/pattern-edits')
    async def sessions(trade_id:str,user=Depends(require_login)):
        return await call(get_pattern_edit_service().sessions,trade_id)

    @app.get('/api/pattern-edits/{session_id}')
    async def read(session_id:str,user=Depends(require_login)):
        return await call(get_pattern_edit_service().read,session_id)

    @app.get('/api/pattern-edits/{session_id}/chart')
    async def chart(session_id:str,kind:str='candidate',user=Depends(require_login)):
        return await call(get_pattern_edit_service().preview_chart,session_id,kind)

    @app.post('/api/pattern-edits/{session_id}/messages')
    async def message(session_id:str,request:Request,user=Depends(require_login)):
        data = await body(request)
        return await call(get_pattern_edit_service().message,session_id,data.get('text'),data.get('selections'),data.get('image'))

    @app.post('/api/pattern-edits/{session_id}/preview')
    async def preview(session_id:str,request:Request,user=Depends(require_login)):
        data = await body(request)
        return await call(get_pattern_edit_service().submit,session_id,'preview',data.get('idempotency_key'))

    @app.post('/api/pattern-edits/{session_id}/compare')
    async def compare(session_id:str,request:Request,user=Depends(require_login)):
        data = await body(request)
        service = get_pattern_edit_service()
        session = await call(service.read,session_id)
        from core.pattern_edit_validation import candle_rows
        from core.market import get_market
        from data.history import load_daily_ohlcv_df
        symbols = data.get('symbols',[])
        if not isinstance(symbols,list) or not 1<=len(symbols)<=20:
            raise HTTPException(422,'Choose 1–20 symbols')
        market = session['trade']['market']
        datasets = []
        for symbol in symbols:
            frame = await run_in_threadpool(load_daily_ohlcv_df,str(symbol),tv_fallback=False,market=market)
            if frame is None:
                raise HTTPException(422,f'No local data for {symbol}')
            frame = frame.loc[data.get('start') or frame.index[0]:data.get('end') or frame.index[-1]]
            datasets.append({'symbol':symbol,'market':market,'timeframe':'1d',
                             'session_timezone':get_market(market).session_tz,
                             'candles':candle_rows(frame,get_market(market).session_tz)})
        return await call(service.submit,session_id,'compare',data.get('idempotency_key'),datasets)

    @app.post('/api/pattern-edits/{session_id}/cancel')
    async def cancel(session_id:str,request:Request,user=Depends(require_login)):
        await body(request)
        return await call(get_pattern_edit_service().cancel,session_id)

    @app.delete('/api/pattern-edits/{session_id}')
    async def discard(session_id:str,request:Request,user=Depends(require_login)):
        await body(request)
        return await call(get_pattern_edit_service().cancel,session_id,True)

    @app.post('/api/pattern-edits/{session_id}/apply')
    async def apply(session_id:str,request:Request,user=Depends(require_login)):
        data = await body(request)
        return await call(get_pattern_edit_service().apply,session_id,data.get('revision_id'),data.get('report_id'),
                          data.get('candidate_sha256'),data.get('idempotency_key'))

    @app.get('/api/patterns/{pattern_id}/versions')
    async def history(pattern_id:str,user=Depends(require_login)):
        return await call(get_pattern_edit_service().history,pattern_id)

    @app.post('/api/patterns/{pattern_id}/rollback')
    async def rollback(pattern_id:str,request:Request,user=Depends(require_login)):
        data=await body(request)
        service=get_pattern_edit_service()
        session=await call(service.read,data.get('session_id'))
        if session['pattern_id']!=pattern_id:
            raise HTTPException(422,'Pattern does not match session')
        return await call(service.rollback,data['session_id'],data.get('version_id'),data.get('reason'))

    @app.get('/api/pattern-activations/{activation_id}')
    async def activation(activation_id:str,user=Depends(require_login)):
        return await call(get_pattern_edit_service().activation_status,activation_id)
