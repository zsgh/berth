/* berth 管理界面（无构建步骤：静态 HTML + Alpine.js，库文件随仓库提交）。
 *
 * 约定：
 * - UI 只调用 berth 自己的 HTTP API，不走后门
 * - token 存在 sessionStorage（不进 URL / 不进日志 / 不进页面源码）
 * - 日志 / stdout / stderr 一律按纯文本渲染
 * - HTML 产物只在 <iframe sandbox> 里展示；图片 / 文本转义后渲染
 *
 * 结构：Alpine 只在根组件上注入 magic，嵌套对象拿不到 $root，
 * 所以子对象和根组件之间一律用 window CustomEvent 通信（不互相引用）。
 */
const TOKEN_KEY = 'berth_token';
const POLL_MS = 3000;

/* ── API 客户端 ─────────────────────────────────────────── */
function authHeaders() {
  const t = sessionStorage.getItem(TOKEN_KEY) || '';
  return t ? { Authorization: 'Bearer ' + t } : {};
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  let body = opts.body;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, Object.assign({}, opts, { headers, body }));
  if (res.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    window.dispatchEvent(new CustomEvent('berth:unauthorized'));
    const err = new Error('unauthorized');
    err.status = 401;
    throw err;
  }
  const ctype = res.headers.get('content-type') || '';
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try {
      if (ctype.includes('json')) detail = (await res.json()).detail || detail;
      else detail = await res.text();
    } catch (e) { /* ignore */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (opts.raw) return res;
  if (ctype.includes('json')) return res.json();
  return res.text();
}

/* SSE：用 fetch + ReadableStream 手动解析（EventSource 不能带 Authorization 头） */
async function sse(path, onEvent, signal) {
  const res = await fetch(path, { headers: authHeaders(), signal });
  if (!res.ok || !res.body) throw new Error('SSE 连接失败: ' + res.status);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = 'message';
      const dataLines = [];
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      const data = dataLines.join('\n');
      let payload = data;
      try { payload = JSON.parse(data); } catch (e) { /* 纯文本 */ }
      onEvent(event, payload);
    }
  }
}

/* ── 小工具 ─────────────────────────────────────────────── */
function human(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  let v = Number(bytes);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${units[i]}`;
}

function parseKV(text) {
  const out = {};
  String(text || '').split(',').forEach((part) => {
    part = part.trim();
    if (!part) return;
    const idx = part.indexOf('=');
    if (idx < 0) out[part] = '';
    else out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  });
  return out;
}

function parseMounts(text) {
  return String(text || '').split(',').map((part) => {
    part = part.trim();
    if (!part) return null;
    const idx = part.indexOf(':');
    if (idx < 0) return null;
    return { source: part.slice(0, idx).trim(), target: part.slice(idx + 1).trim(), read_only: true };
  }).filter(Boolean);
}

/* 单次最多展示最后 1MB，超出部分提示去下载 */
const MAX_DISPLAY = 1024 * 1024;

function tailText(text) {
  const s = String(text || '');
  if (s.length <= MAX_DISPLAY) return s;
  return '…（只显示最后 1MB，完整内容请用下载接口取）\n' + s.slice(-MAX_DISPLAY);
}

/* 当前路由参数（#/runs/xxx → xxx） */
function routeParam(index) {
  const parts = (location.hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  return parts[index] ? decodeURIComponent(parts[index]) : null;
}

/* 子对象 → 根组件的通信（不互相持有引用） */
function say(msg, kind) {
  window.dispatchEvent(new CustomEvent('berth:toast', { detail: { msg, kind: kind || 'ok' } }));
}

function askConfirm(opts) {
  return new Promise((resolve) => {
    window.dispatchEvent(new CustomEvent('berth:confirm', { detail: Object.assign({}, opts, { resolve }) }));
  });
}

/* ── 根组件 ─────────────────────────────────────────────── */
function shell() {
  return {
    tokenInput: '',
    loggedIn: false,
    loginError: '',
    busy: false,
    route: { name: 'overview', param: null },
    info: null,
    stats: null,
    toast: '',
    toastKind: 'ok',
    toastTimer: null,
    serviceCount: 0,
    runCount: 0,
    healthClass: 'bad',
    healthText: '未连接',
    confirm: {
      open: false, title: '', message: '', name: '', needName: true,
      input: '', okText: '', error: '', resolve: null,
      accept() {
        if (this.needName && (this.input || '').trim() !== this.name) {
          this.error = '输入的名称与目标不一致，未执行';
          return;
        }
        this.open = false;
        const done = this.resolve;
        this.resolve = null;
        if (done) done(true);
      },
      cancel() {
        this.open = false;
        const done = this.resolve;
        this.resolve = null;
        if (done) done(false);
      },
    },

    services: {
      rows: [], showAll: false, labelFilter: '', createOpen: false,
      form: { name: '', template: 'redis', env: '', initSql: '', labels: '' },
      async load() {
        const q = new URLSearchParams();
        if (this.showAll) q.set('all', 'true');
        if (this.labelFilter.trim()) q.set('label', this.labelFilter.trim());
        this.rows = await api('/services?' + q.toString());
      },
      async create() {
        const f = this.form;
        if (!f.name) return say('请填服务名', 'err');
        const body = { template: f.template };
        const labels = parseKV(f.labels);
        if (Object.keys(labels).length) body.labels = labels;
        let env = {};
        if (f.env.trim()) {
          try { env = JSON.parse(f.env); } catch (e) { return say('env 不是合法 JSON', 'err'); }
        }
        if (Object.keys(env).length) body.overrides = { env };
        if (f.initSql.trim()) body.init = { sql: f.initSql };
        try {
          await api('/services/' + encodeURIComponent(f.name), { method: 'PUT', json: body });
          say('服务已就绪：' + f.name);
          this.createOpen = false;
          f.initSql = '';
          await this.load();
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async act(s, op, query) {
        const name = s.name || s.service_id;
        if (op === 'delete') {
          const ok = await askConfirm({
            title: '删除服务 ' + name,
            message: query ? '会连同数据卷一起删除，无法恢复。' : '只删记录，数据卷保留。',
            name: name, okText: '删除',
          });
          if (!ok) return;
          try {
            await api('/services/' + encodeURIComponent(name) + (query ? '?' + query : ''), { method: 'DELETE' });
            say('已删除 ' + name);
          } catch (e) { say(String(e.message || e), 'err'); }
        } else {
          try {
            await api('/services/' + encodeURIComponent(name) + '/' + op, { method: 'POST' });
            say(name + ' → ' + op + ' 完成');
          } catch (e) { say(String(e.message || e), 'err'); }
        }
        await this.load();
      },
      async confirmReset(s) {
        const name = s.name || s.service_id;
        const ok = await askConfirm({
          title: '重置服务 ' + name,
          message: '停止服务 → 清空数据卷 → 重放初始化 → 重新启动。数据会回到 init 后的状态。',
          name: name, okText: '重置',
        });
        if (!ok) return;
        try {
          const r = await api('/services/' + encodeURIComponent(name) + '/reset', { method: 'POST' });
          say('重置完成（' + (r.reset_duration_ms / 1000).toFixed(2) + 's）');
        } catch (e) { say(String(e.message || e), 'err'); }
        await this.load();
      },
    },

    service: {
      detail: null, logText: '', tail: 200, follow: false, controller: null,
      async load() {
        try {
          this.detail = await api('/services/' + encodeURIComponent(routeParam(1)));
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      clip: tailText,
      async loadLogs() {
        try {
          const r = await api('/services/' + encodeURIComponent(routeParam(1)) + '/logs?tail=' + this.tail);
          this.logText = r.text || '';
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async startFollow() {
        this.stopFollow();
        this.controller = new AbortController();
        const path = '/services/' + encodeURIComponent(routeParam(1)) + '/logs?follow=true&tail=' + this.tail;
        try {
          await sse(path, (event, payload) => {
            if (event === 'error') { say(String(payload.detail || payload), 'err'); return; }
            if (event === 'done') return;
            this.logText += (payload.line || payload) + '\n';
          }, this.controller.signal);
        } catch (e) {
          if (e.name !== 'AbortError') say(String(e.message || e), 'err');
        }
      },
      stopFollow() {
        if (this.controller) { try { this.controller.abort(); } catch (e) { /* ignore */ } }
        this.controller = null;
      },
      toggleFollow() {
        this.follow = !this.follow;
        if (this.follow) this.startFollow();
        else this.stopFollow();
      },
    },

    runs: {
      rows: [], filter: { status: '', image: '', label: '' },
      async load() {
        const q = new URLSearchParams();
        if (this.filter.status) q.set('status', this.filter.status);
        if (this.filter.image) q.set('image', this.filter.image);
        if (this.filter.label.trim()) q.set('label', this.filter.label.trim());
        const r = await api('/scripts?' + q.toString());
        this.rows = r.runs || [];
      },
    },

    run: {
      detail: null, stdout: '', stderr: '', files: [], controller: null,
      previewFile: null, previewKind: '', previewUrl: '', previewText: '',
      async load() {
        const id = routeParam(1);
        try {
          this.detail = await api('/scripts/' + encodeURIComponent(id));
          this.stdout = this.detail.stdout || '';
          this.stderr = this.detail.stderr || '';
          if (this.detail.status === 'running' && !this.controller) this.startFollow();
        } catch (e) { say(String(e.message || e), 'err'); }
        await this.loadFiles();
      },
      clip: tailText,
      async loadFiles() {
        try {
          const r = await api('/scripts/' + encodeURIComponent(routeParam(1)) + '/tree');
          this.files = r.files || [];
        } catch (e) { this.files = []; }
      },
      async startFollow() {
        this.stopFollow();
        this.controller = new AbortController();
        const path = '/scripts/' + encodeURIComponent(routeParam(1)) + '/stream';
        try {
          await sse(path, (event, payload) => {
            if (event === 'stdout') this.stdout += payload.text || '';
            else if (event === 'stderr') this.stderr += payload.text || '';
            else if (event === 'done') { this.stopFollow(); this.load(); }
          }, this.controller.signal);
        } catch (e) {
          if (e.name !== 'AbortError') say(String(e.message || e), 'err');
        }
      },
      stopFollow() {
        if (this.controller) { try { this.controller.abort(); } catch (e) { /* ignore */ } }
        this.controller = null;
      },
      async cancel() {
        try {
          await api('/scripts/' + encodeURIComponent(routeParam(1)) + '/cancel', { method: 'POST' });
          say('已取消');
          await this.load();
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async clearWorkspace() {
        const id = routeParam(1);
        const ok = await askConfirm({
          title: '清空 workspace', message: '任务 ' + id + ' 的 /workspace 会被清空。',
          name: id, okText: '清空',
        });
        if (!ok) return;
        try {
          await api('/scripts/' + encodeURIComponent(id) + '/clear', { method: 'POST' });
          say('workspace 已清空');
          await this.loadFiles();
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async downloadAll() {
        const id = routeParam(1);
        await this.download('/scripts/' + encodeURIComponent(id) + '/files', id + '.tar');
      },
      async download(f) {
        await this.download('/scripts/' + encodeURIComponent(routeParam(1)) + '/files/' + f.path, f.name);
      },
      async download(path, filename) {
        try {
          const res = await api(path, { raw: true });
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async preview(f) {
        this.previewFile = f;
        this.previewUrl = '';
        this.previewText = '';
        const path = '/scripts/' + encodeURIComponent(routeParam(1)) + '/files/' + f.path;
        if (f.mime && f.mime.startsWith('image/')) {
          this.previewKind = 'image';
          try {
            const res = await api(path + '?preview=1', { raw: true });
            this.previewUrl = URL.createObjectURL(await res.blob());
          } catch (e) { this.previewKind = 'other'; }
        } else if (f.mime === 'text/html' || f.mime === 'application/xhtml+xml') {
          this.previewKind = 'html';
          // HTML 只在 sandbox iframe 里展示（服务端也带严格 CSP）
          try {
            const res = await api(path + '?preview=1', { raw: true });
            this.previewUrl = URL.createObjectURL(await res.blob());
          } catch (e) { this.previewKind = 'other'; }
        } else if (f.mime && (f.mime.startsWith('text/') || f.mime === 'application/json')) {
          this.previewKind = 'text';
          try {
            this.previewText = await api(path + '?preview=1');
          } catch (e) { this.previewKind = 'other'; }
        } else {
          this.previewKind = 'other';
        }
      },
    },

    newRun: {
      image: 'base', language: '', timeout: 60, memory_mb: 512, cpu_percent: 100,
      processes: null, task_id: '', code: 'print("hello from berth")', cmd: '',
      networkMode: 'host', networkAllow: '', labels: '', mounts: '', isAsync: true,
      async submit() {
        const body = {
          image: this.image, timeout: this.timeout,
          memory_mb: this.memory_mb, cpu_percent: this.cpu_percent,
        };
        if (this.language) body.language = this.language;
        if (this.task_id.trim()) body.task_id = this.task_id.trim();
        if (this.processes) body.processes = this.processes;
        if (this.cmd.trim()) {
          try { body.cmd = JSON.parse(this.cmd); } catch (e) { return say('cmd 不是合法 JSON 数组', 'err'); }
        } else {
          body.code = this.code;
        }
        body.async = !!this.isAsync;
        body.network = this.networkMode === 'allow'
          ? { mode: 'allow', allow: this.networkAllow.split(',').map((s) => s.trim()).filter(Boolean) }
          : { mode: this.networkMode, allow: [] };
        const labels = parseKV(this.labels);
        if (Object.keys(labels).length) body.labels = labels;
        const mounts = parseMounts(this.mounts);
        if (mounts.length) body.mounts = mounts;
        try {
          const r = await api('/scripts/run', { method: 'POST', json: body });
          say('已提交：' + r.task_id);
          location.hash = '#/runs/' + encodeURIComponent(r.task_id);
        } catch (e) { say(String(e.message || e), 'err'); }
      },
    },

    volumes: {
      rows: [],
      async load() { this.rows = await api('/volumes'); },
      async remove(v) {
        const ok = await askConfirm({
          title: '清理孤儿卷', message: '删除 ' + v.path + '（无法恢复）。',
          name: v.service_id, okText: '清理',
        });
        if (!ok) return;
        try {
          await api('/volumes/' + encodeURIComponent(v.service_id), { method: 'DELETE' });
          say('已清理 ' + v.service_id);
        } catch (e) { say(String(e.message || e), 'err'); }
        await this.load();
      },
    },

    images: {
      rows: [], builds: [], buildOpen: false, canBuild: false, canDelete: true,
      form: { name: '', base: 'base', packages: '', pip: '', commands: '', force: false },
      async load() {
        this.rows = await api('/images');
        try { this.builds = await api('/images/builds'); } catch (e) { this.builds = []; }
      },
      async submitBuild() {
        const f = this.form;
        if (!f.name.trim()) return say('请填镜像名', 'err');
        const body = {
          name: f.name.trim(),
          base: f.base,
          packages: f.packages.split(/[\s,]+/).filter(Boolean),
          pip_packages: f.pip.split(/[\s,]+/).filter(Boolean),
          commands: f.commands.split('\n').map((x) => x.trim()).filter(Boolean),
          force: !!f.force,
        };
        try {
          const r = await api('/images', { method: 'POST', json: body });
          say('开始制作镜像：' + r.name + '（' + r.build_id + '）');
          this.buildOpen = false;
          await this.load();
        } catch (e) { say(String(e.message || e), 'err'); }
      },
      async remove(img) {
        const ok = await askConfirm({
          title: '删除镜像 ' + img.name,
          message: '会删除目录 ' + img.path + '（不可恢复）。用这个镜像的服务要先停掉/删掉。',
          name: img.name, okText: '删除',
        });
        if (!ok) return;
        try {
          await api('/images/' + encodeURIComponent(img.name), { method: 'DELETE' });
          say('已删除镜像 ' + img.name);
        } catch (e) { say(String(e.message || e), 'err'); }
        await this.load();
      },
    },

    templates: {
      names: [], text: '', current: '', error: '',
      async load() { this.names = await api('/templates'); },
      async show(name) {
        this.current = name; this.error = '';
        try { this.text = JSON.stringify(await api('/templates/' + encodeURIComponent(name)), null, 2); }
        catch (e) { this.error = String(e.message || e); }
      },
      async validate(name) {
        this.current = name; this.error = '';
        try {
          const tpl = await api('/templates/' + encodeURIComponent(name));
          this.text = JSON.stringify(tpl, null, 2);
          say('模板 ' + name + ' 校验通过');
        } catch (e) { this.error = String(e.message || e); say('校验失败：' + this.error, 'err'); }
      },
    },

    events: { rows: [], async load() { this.rows = await api('/events'); } },

    /* ── 生命周期 ── */
    async boot() {
      window.addEventListener('hashchange', () => this.onRoute());
      window.addEventListener('berth:unauthorized', () => {
        this.loggedIn = false;
        this.healthClass = 'bad';
        this.healthText = 'token 失效';
        this.say('token 无效或已失效，请重新登录', 'err');
      });
      window.addEventListener('berth:toast', (e) => {
        this.toast = e.detail.msg;
        this.toastKind = e.detail.kind === 'err' ? 'err' : 'ok';
        clearTimeout(this.toastTimer);
        this.toastTimer = setTimeout(() => { this.toast = ''; }, 4000);
      });
      window.addEventListener('berth:confirm', (e) => {
        Object.assign(this.confirm, {
          open: true, title: '确认', message: '', name: '', needName: true,
          input: '', okText: '确认', error: '', resolve: null,
        }, e.detail);
      });
      const saved = sessionStorage.getItem(TOKEN_KEY);
      if (saved) {
        this.tokenInput = saved;
        await this.login();
      } else {
        // 服务端没配 BERTH_TOKEN（不鉴权）时直接进，不用登录
        try {
          this.info = await api('/system/info');
          this.loggedIn = true;
          this.healthClass = 'ok';
          this.healthText = 'berth 正常 · v' + (this.info.version || '') + '（未启用鉴权）';
        } catch (e) { /* 需要 token：停在登录页 */ }
      }
      this.onRoute();
      this.startPolling();
    },

    async login(event) {
      if (event && event.target && event.target.reportValidity && !event.target.reportValidity()) return;
      this.busy = true;
      this.loginError = '';
      const value = (this.tokenInput || '').trim();
      if (value) sessionStorage.setItem(TOKEN_KEY, value);
      try {
        this.info = await api('/system/info');
        this.loggedIn = true;
        this.healthClass = 'ok';
        this.healthText = 'berth 正常 · v' + (this.info.version || '');
        this.onRoute();
      } catch (e) {
        this.loggedIn = false;
        this.healthClass = 'bad';
        this.healthText = '连接失败';
        this.loginError = e.status === 401 ? 'token 不正确' : String(e.message || e);
        sessionStorage.removeItem(TOKEN_KEY);
      } finally {
        this.busy = false;
      }
    },

    logout() {
      sessionStorage.removeItem(TOKEN_KEY);
      this.loggedIn = false;
      this.tokenInput = '';
    },

    onRoute() {
      const hash = (location.hash || '#/').replace(/^#/, '');
      const parts = hash.split('/').filter(Boolean);
      const section = parts[0] || 'overview';
      const param = parts[1] ? decodeURIComponent(parts[1]) : null;
      // #/runs/{task_id} → 详情；#/services/{name} → 详情；#/run/new → 手动运行
      let page = section;
      if (section === 'runs' && param) page = 'rundetail';
      else if (section === 'services' && param) page = 'servicedetail';
      else if (section === 'run' && param === 'new') page = 'runnew';
      this.route = { name: page, section, param };
      this.loadRoute();
    },

    async loadRoute() {
      if (!this.loggedIn) return;
      const name = this.route.name;
      try {
        if (name === 'overview') {
          this.stats = await api('/system/stats');
        } else if (name === 'services') {
          await this.services.load();
          this.serviceCount = this.services.rows.length;
        } else if (name === 'servicedetail') {
          this.service.stopFollow();
          this.service.follow = false;
          await this.service.load();
          await this.service.loadLogs();
        } else if (name === 'runs') {
          await this.runs.load();
          this.runCount = this.runs.rows.length;
        } else if (name === 'rundetail') {
          this.run.stopFollow();
          await this.run.load();
        } else if (name === 'volumes') {
          await this.volumes.load();
        } else if (name === 'images') {
          await this.images.load();
          const mgmt = (this.info && this.info.image_mgmt) || {};
          this.images.canBuild = !!mgmt.build;
          this.images.canDelete = mgmt.delete !== false;
        } else if (name === 'templates') {
          await this.templates.load();
        } else if (name === 'events') {
          await this.events.load();
        }
      } catch (e) {
        this.say(String(e.message || e), 'err');
      }
    },

    startPolling() {
      setInterval(() => {
        if (document.visibilityState !== 'visible' || !this.loggedIn) return;
        const name = this.route.name;
        const p = (fn) => fn().catch(() => {});
        if (name === 'overview') {
          p(async () => { this.stats = await api('/system/stats'); });
        } else if (name === 'services') {
          p(async () => { await this.services.load(); this.serviceCount = this.services.rows.length; });
        } else if (name === 'runs') {
          p(async () => { await this.runs.load(); this.runCount = this.runs.rows.length; });
        } else if (name === 'servicedetail') {
          p(() => this.service.load());
        } else if (name === 'rundetail') {
          p(() => this.run.load());
        } else if (name === 'volumes') {
          p(() => this.volumes.load());
        } else if (name === 'images') {
          p(() => this.images.load());
        }
      }, POLL_MS);
    },

    say(msg, kind) {
      this.toast = msg;
      this.toastKind = kind === 'err' ? 'err' : 'ok';
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toast = ''; }, 4000);
    },

    askConfirm(opts) {
      Object.assign(this.confirm, {
        open: true, title: '确认', message: '', name: '', needName: true,
        input: '', okText: '确认', error: '', resolve: null,
      }, opts || {});
      return new Promise((resolve) => { this.confirm.resolve = resolve; });
    },

    human,
  };
}

document.addEventListener('alpine:init', () => {
  if (window.Alpine && window.Alpine.data) window.Alpine.data('shell', shell);
});
