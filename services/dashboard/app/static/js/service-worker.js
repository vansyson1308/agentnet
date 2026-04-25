const CACHE_NAME = 'agentnet-static-v1';
const STATIC_ASSET_PATTERN = /^\/static\//;

self.addEventListener('install', (event) => {
    // Activate immediately without waiting for existing clients
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    // Clean up old caches
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;

    // Only handle GET requests for static assets
    if (request.method !== 'GET' || !STATIC_ASSET_PATTERN.test(new URL(request.url).pathname)) {
        return;
    }

    event.respondWith(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.match(request).then((cachedResponse) => {
                // Cache-first: return cached response if available
                if (cachedResponse) {
                    return cachedResponse;
                }

                // Otherwise fetch from network and cache the response
                return fetch(request).then((networkResponse) => {
                    // Don't cache non-ok responses or opaque responses
                    if (networkResponse.ok || networkResponse.type === 'opaque') {
                        cache.put(request, networkResponse.clone());
                    }
                    return networkResponse;
                });
            });
        })
    );
});