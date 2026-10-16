/* DMT Class 01 service worker —— 离线优先层
 *
 * 策略：
 *  1) 页面导航（打开网站、刷新）：先试网络（3.5 秒），成功就用最新的并写入缓存；
 *     超时或失败则回落到缓存里的 index.html。
 *     —— 源站正常时永远看到最新版；源站被打进黑洞或 504 时，网站仍然打得开。
 *  2) /assets/ 与根目录图标：缓存优先（文件名不变即内容不变）。
 *  3) /api/ 动态接口：网络优先，3 秒超时后立刻返回 503 JSON，
 *     绝不让页面卡在"加载中"。
 *  4) 跨域请求（字体、高德、AI 服务）一律放行，不拦截。
 *
 * 改缓存清单或版本号时，记得把 VERSION 一起改掉，否则激活阶段不会清理旧缓存。
 */
const VERSION = 'dmt-shell-v3';
const API_CACHE = 'dmt-api-v1';
/* 只缓存公开只读接口；会员与个人数据（/api/me、消息、反馈等）一律不缓存，
   避免同一台设备上的下一个使用者看到上一个人的数据。 */
const API_CACHEABLE = ['/api/news', '/api/events', '/api/schedule', '/api/folders', '/api/team', '/api/config'];
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon-512.png", "/icon-maskable-512.png", "/assets/favicon.svg", "/assets/favicon-192.png", "/assets/favicon-32.png", "/assets/apple-touch-icon.png", "/assets/paris-chateau.svg", "/assets/paris-garden.svg", "/assets/paris-nature.svg", "/assets/paris-skyline.svg"];
const NAV_TIMEOUT = 3500;
const API_TIMEOUT = 3000;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    await Promise.all(SHELL.map(async (url) => {
      try {
        await cache.add(new Request(url, { cache: 'reload' }));
      } catch (err) {
        /* 单个文件失败（比如某张图暂时 404）不影响整体安装 */
      }
    }));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== VERSION && k !== API_CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); }
    );
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      const cache = await caches.open(VERSION);
      try {
        const fresh = await withTimeout(fetch(req), NAV_TIMEOUT);
        if (fresh && fresh.ok) cache.put('/index.html', fresh.clone());
        return fresh;
      } catch (err) {
        const hit = (await cache.match('/index.html')) || (await cache.match('/'));
        if (hit) return hit;
        return new Response('离线且暂无缓存副本 / offline, no cached copy', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      }
    })());
    return;
  }

  if (url.pathname.indexOf('/api/') === 0) {
    event.respondWith((async () => {
      const cacheable = API_CACHEABLE.some(function (p) {
        return url.pathname === p || url.pathname.indexOf(p + '/') === 0;
      });
      const cache = await caches.open(API_CACHE);
      try {
        const fresh = await withTimeout(fetch(req), API_TIMEOUT);
        if (fresh && fresh.ok && cacheable) cache.put(req, fresh.clone());
        return fresh;
      } catch (err) {
        if (cacheable) {
          const hit = await cache.match(req, { ignoreSearch: true });
          if (hit) {
            const text = await hit.text();
            return new Response(text, {
              status: 200,
              headers: {
                'Content-Type': 'application/json; charset=utf-8',
                'Cache-Control': 'no-store',
                'X-Offline-Cache': '1'
              }
            });
          }
        }
        return new Response(JSON.stringify({ error: 'offline', offline: true }), {
          status: 503,
          headers: { 'Content-Type': 'application/json; charset=utf-8' }
        });
      }
    })());
    return;
  }

  event.respondWith((async () => {
    const cache = await caches.open(VERSION);
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    try {
      const res = await fetch(req);
      if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
      return res;
    } catch (err) {
      return new Response('', { status: 504 });
    }
  })());
});
