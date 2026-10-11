"""Bounded incremental SSE reader with per-attempt timing and usage metrics."""
import codecs
import http.client
import json
import re
import socket
import ssl
import time
from urllib import error, request

MAX_BODY = 16 * 1024 * 1024


class UpstreamError(Exception):
    def __init__(self, message, code='upstream', retryable=False, metrics=None):
        super().__init__(message)
        self.code, self.retryable, self.metrics = code, retryable, metrics or {}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise UpstreamError('接口返回重定向，请填写最终地址', 'redirect')


def usage_metrics(usage):
    if not isinstance(usage, dict):
        return {}
    details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details')
    if not isinstance(details,dict):
        details = {}
    values = {'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens')),
              'output_tokens': usage.get('output_tokens', usage.get('completion_tokens')),
              'cached_tokens': details.get('cached_tokens')}
    result = {key: value for key, value in values.items() if type(value) is int and value >= 0}
    if result.get('cached_tokens', 0) > result.get('input_tokens', float('inf')):
        result.pop('cached_tokens', None)
    return result


class SSEFramer:
    """Incremental CR, LF, and CRLF framing, including split CRLF boundaries."""
    def __init__(self):
        self.parts, self.lines, self.skip_lf = [], [], False

    def feed(self, text, final=False):
        if self.skip_lf and text:
            text = text.removeprefix('\n')
            self.skip_lf = False
        blocks, cursor = [], 0
        for match in re.finditer(r'\r\n|\r|\n',text):
            self.parts.append(text[cursor:match.start()])
            line = ''.join(self.parts)
            self.parts = []
            if line:
                self.lines.append(line)
            else:
                blocks.append('\n'.join(self.lines))
                self.lines = []
            cursor = match.end()
            self.skip_lf = match.group()=='\r' and cursor==len(text)
        if cursor<len(text):
            self.parts.append(text[cursor:])
        if final:
            line = ''.join(self.parts)
            if line:
                self.lines.append(line)
            if self.lines:
                blocks.append('\n'.join(self.lines))
            self.parts,self.lines = [],[]
        return blocks


class StreamParser:
    def __init__(self, protocol, on_content):
        self.protocol, self.on_content = protocol, on_content
        self.text, self.completed, self.model, self.usage = [], False, '', {}
        self.terminal = False
        self.final_response = None

    def event(self, block):
        payload = '\n'.join(line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:'))
        if not payload:
            return
        if payload.strip() == '[DONE]':
            self.terminal = True
            return
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            raise UpstreamError('流式事件格式无效', 'invalid_stream', True)
        if not isinstance(data, dict):
            raise UpstreamError('流式事件格式无效', 'invalid_stream', True)
        kind = data.get('type', '')
        if data.get('error') or kind in ('error', 'response.failed', 'response.incomplete'):
            detail = data.get('error') or (data.get('response') or {}).get('error') or {}
            code = detail.get('code', 'upstream') if isinstance(detail, dict) else 'upstream'
            # Do not propagate arbitrary upstream strings into public error codes.
            code = code if code in ('server_error','rate_limit_exceeded','invalid_api_key','invalid_request_error',
                                    'insufficient_quota','model_not_found','context_length_exceeded') else 'upstream'
            raise UpstreamError('上游返回错误或未完整结束', code, code in ('server_error','rate_limit_exceeded'))
        if self.protocol == 'responses':
            if kind in ('response.output_text.delta','response.reasoning_summary_text.delta','response.reasoning_text.delta'):
                delta = data.get('delta', '')
                if isinstance(delta, str) and delta.strip():
                    self.on_content()
                if kind == 'response.output_text.delta' and isinstance(delta, str):
                    self.text.append(delta)
            if kind == 'response.completed':
                self.final_response = data.get('response') or {}
                self.completed = self.final_response.get('status') == 'completed'
                self.terminal = True
                self.model = str(self.final_response.get('model',''))
                self.usage = usage_metrics(self.final_response.get('usage'))
                if self.completed and any(part.get('type')=='output_text' and isinstance(part.get('text'),str) and part['text'].strip()
                                          for item in self.final_response.get('output',[]) if isinstance(item,dict) and item.get('type')=='message'
                                          for part in item.get('content',[]) if isinstance(part,dict)):
                    self.on_content()
        else:
            self.model = str(data.get('model', self.model))
            if data.get('usage'):
                self.usage = usage_metrics(data['usage'])
                if self.completed:
                    self.terminal = True
            for choice in data.get('choices') or []:
                if choice.get('index', 0) != 0:
                    continue
                delta = choice.get('delta') or {}
                text = delta.get('content', '')
                reasoning = delta.get('reasoning_content') or delta.get('reasoning') or ''
                if (isinstance(text,str) and text.strip()) or (isinstance(reasoning,str) and reasoning.strip()):
                    self.on_content()
                if isinstance(text, str):
                    self.text.append(text)
                if choice.get('finish_reason'):
                    if choice['finish_reason'] != 'stop':
                        raise UpstreamError('输出未完整结束，可能达到输出上限', 'incomplete')
                    self.completed = True

    def finish(self):
        text = ''.join(self.text)
        if self.protocol == 'responses' and self.final_response:
            output = '\n'.join(part.get('text','') for item in self.final_response.get('output', [])
                               if item.get('type')=='message' for part in item.get('content', []) if part.get('type')=='output_text')
            text = output or text
        if not self.completed:
            raise UpstreamError('流式响应中断，缺少完整结束事件', 'incomplete_stream', True)
        if not text.strip():
            raise UpstreamError('上游未返回有效文本', 'empty', True)
        return text, self.usage, self.model


def parse_json(data, protocol):
    if not isinstance(data, dict) or data.get('error'):
        raise UpstreamError('上游返回无效响应', 'invalid_response')
    if protocol == 'responses':
        if data.get('status') != 'completed':
            raise UpstreamError('上游响应未完整结束', 'incomplete')
        text = '\n'.join(p.get('text','') for item in data.get('output', []) if item.get('type')=='message'
                         for p in item.get('content', []) if p.get('type')=='output_text')
    else:
        choice = (data.get('choices') or [{}])[0]
        if choice.get('finish_reason') != 'stop':
            raise UpstreamError('上游响应未完整结束', 'incomplete')
        text = (choice.get('message') or {}).get('content', '')
        if isinstance(text, list):
            text = '\n'.join(p.get('text','') for p in text if p.get('type')=='text')
    if not isinstance(text,str) or not text.strip():
        raise UpstreamError('上游未返回有效文本', 'empty', True)
    return text, usage_metrics(data.get('usage')), str(data.get('model',''))


def call_model(config, prompt, on_first=None):
    started, clock = time.time(), time.monotonic()
    metrics = {'started':started, 'http_status':None, 'first_token_seconds':None}
    deadline = clock + config['timeout_seconds']
    first_limit = min(config.get('first_token_timeout',120), config['timeout_seconds'])
    protocol = config['protocol']
    common = {'model':config['model'], 'stream':True}
    if protocol == 'responses':
        body = dict(common, input=[{'role':'user','content':prompt}], reasoning={'effort':config['effort']},
                    max_output_tokens=config['max_output_tokens'], store=False)
        suffix = '/responses'
    else:
        body = dict(common, messages=[{'role':'user','content':prompt}], reasoning_effort=config['effort'],
                    max_completion_tokens=config['max_output_tokens'], stream_options={'include_usage':True})
        suffix = '/chat/completions'
    req = request.Request(config['base_url'] + suffix, json.dumps(body).encode(),
                          {'Authorization':'Bearer '+config['api_key'], 'Content-Type':'application/json',
                           'Accept':'text/event-stream','User-Agent':'llm-dashboard/2.0'})
    def content():
        if metrics['first_token_seconds'] is None:
            metrics['first_token_seconds'] = time.monotonic()-clock
            if on_first:
                on_first(metrics['first_token_seconds'])
    parser = None
    try:
        # urllib's open timeout also applies to TLS and waiting for response
        # headers, not just connecting. Some streaming gateways hold headers
        # until the first content arrives, so a 20s cap ignored the configured
        # first-content budget and caused premature retries.
        open_budget = min(deadline,clock+first_limit)-time.monotonic()
        if open_budget<=0:
            raise TimeoutError()
        with request.build_opener(NoRedirect()).open(req, timeout=open_budget) as response:
            metrics['http_status'] = response.status
            streaming = 'text/event-stream' in response.headers.get('Content-Type','').lower()
            parser = StreamParser(protocol, content)
            framer = SSEFramer()
            decoder = codecs.getincrementaldecoder('utf-8-sig')()
            raw, size, completed_at = [], 0, None
            response_socket = response.fp.raw._sock
            while not response.isclosed():
                remaining = deadline-time.monotonic()
                if streaming and metrics['first_token_seconds'] is None and not parser.completed:
                    remaining = min(remaining, clock+first_limit-time.monotonic())
                if completed_at is not None:
                    # Chat usage may trail finish_reason, but an open keepalive must
                    # not turn a complete successful answer into a request failure.
                    remaining = min(remaining, completed_at+2-time.monotonic())
                remaining = min(remaining, config.get('idle_timeout',60))
                if remaining <= 0:
                    if streaming and parser.completed:
                        break
                    raise TimeoutError()
                response_socket.settimeout(remaining)
                try:
                    chunk = response.read1(min(65536,MAX_BODY+1-size))
                except (TimeoutError,socket.timeout,ConnectionError,http.client.IncompleteRead):
                    if streaming and parser.completed:
                        break
                    raise
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY:
                    raise UpstreamError('上游响应超过大小限制', 'too_large')
                if not streaming:
                    raw.append(chunk)
                    continue
                for block in framer.feed(decoder.decode(chunk)):
                    parser.event(block)
                    if parser.completed and completed_at is None:
                        completed_at = time.monotonic()
                    if parser.terminal:
                        break
                if parser.terminal:
                    break
            if streaming:
                if not parser.terminal:
                    for block in framer.feed(decoder.decode(b'',final=True),final=True):
                        parser.event(block)
                text, usage, model = parser.finish()
            else:
                # Nonstream fallback has total duration only: no fabricated TTFT.
                text, usage, model = parse_json(json.loads(b''.join(raw)),protocol)
            metrics.update(usage)
            metrics.update(finished=time.time(),duration_seconds=time.monotonic()-clock,status='success')
            return text, metrics, model
    except Exception as exc:
        if parser is not None:
            metrics.update(parser.usage)
        metrics.update(finished=time.time(),duration_seconds=time.monotonic()-clock,status='failed')
        if isinstance(exc, error.HTTPError):
            metrics['http_status'] = exc.code
            messages = {401:'API Key 无效或已过期',403:'接口拒绝访问',404:'接口或模型不存在',429:'请求限流或额度不足'}
            failure = UpstreamError(messages.get(exc.code,f'上游 HTTP {exc.code}'),f'http_{exc.code}',exc.code in (408,429) or exc.code>=500)
            exc.close()
        elif isinstance(exc, (TimeoutError, socket.timeout)) or (isinstance(exc,error.URLError) and isinstance(exc.reason,(TimeoutError,socket.timeout))):
            metrics['status'] = 'timeout'
            failure = UpstreamError('上游响应超时','timeout',True)
        elif isinstance(exc, UpstreamError):
            failure = exc
        elif isinstance(exc,ssl.SSLCertVerificationError) or (isinstance(exc,error.URLError) and isinstance(exc.reason,ssl.SSLCertVerificationError)):
            failure = UpstreamError('上游证书验证失败','tls')
        elif isinstance(exc,(error.URLError,ConnectionError,http.client.IncompleteRead,OSError)):
            failure = UpstreamError('无法建立或保持上游连接','connection',True)
        else:
            failure = UpstreamError('上游响应格式无效','invalid_response',True)
        failure.metrics = metrics
        raise failure from None
