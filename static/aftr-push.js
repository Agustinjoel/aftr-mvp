(function () {
  'use strict';

  var VAPID_PUBLIC_KEY = 'BNUX2vp8zibrk3-ab3TYl0WQEb8yaBi-3_S8GRbUsEhFfOlnsh38Snx0QVyk_Cq8ogv1b3R4yF2yImpuUV1tV8Q';

  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var rawData = window.atob(base64);
    var outputArray = new Uint8Array(rawData.length);
    for (var i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  function sendSubscriptionToServer(sub) {
    var json = sub.toJSON();
    return fetch('/user/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
    });
  }

  function clearServerSubscriptions() {
    return fetch('/user/push/clear', {
      method: 'DELETE',
      credentials: 'include',
    }).catch(function () {});
  }

  function subscribe(reg, onSuccess, onError) {
    reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
    }).then(function (sub) {
      return sendSubscriptionToServer(sub);
    }).then(function (res) {
      return res.json();
    }).then(function (data) {
      if (data.ok) {
        if (onSuccess) onSuccess();
      } else {
        if (onError) onError('Error servidor: ' + (data.error || 'desconocido'));
      }
    }).catch(function (err) {
      if (onError) onError(err && err.message ? err.message : 'Error desconocido');
    });
  }

  function initPush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;

    navigator.serviceWorker.register('/sw.js').then(function (reg) {
      var reactivateBtn = document.getElementById('push-reactivate-btn');

      if (Notification.permission === 'granted') {
        // Wires up the reactivate button click handler (button stays hidden unless needed)
        function wireReactivateBtn() {
          if (!reactivateBtn) return;
          if (reactivateBtn._wired) return;
          reactivateBtn._wired = true;
          reactivateBtn.addEventListener('click', function () {
            reactivateBtn.disabled = true;
            reactivateBtn.textContent = '⏳ Activando...';
            reg.pushManager.getSubscription().then(function (existing) {
              var doSubscribe = function () {
                clearServerSubscriptions().then(function () {
                  subscribe(reg, function () {
                    reactivateBtn.textContent = '✅ Activadas';
                    setTimeout(function () { reactivateBtn.style.display = 'none'; }, 3000);
                  }, function (err) {
                    reactivateBtn.disabled = false;
                    var msg = (err && err.message) ? err.message : String(err);
                    reactivateBtn.textContent = '❌ ' + msg;
                    console.error('[AFTR push] subscribe error:', err);
                  });
                });
              };
              if (existing) {
                existing.unsubscribe().then(doSubscribe).catch(doSubscribe);
              } else {
                doSubscribe();
              }
            });
          });
        }

        reg.pushManager.getSubscription().then(function (existing) {
          if (existing) {
            // Validate subscription with server; refresh if rejected
            sendSubscriptionToServer(existing).then(function (res) {
              return res.json();
            }).then(function (data) {
              if (!data.ok) {
                // Server rejected — silently resubscribe
                existing.unsubscribe().then(function () {
                  return clearServerSubscriptions();
                }).then(function () {
                  subscribe(reg, null, function () {
                    // Auto-resubscribe failed — show button as fallback
                    wireReactivateBtn();
                    if (reactivateBtn) reactivateBtn.style.display = '';
                  });
                });
              }
              // data.ok === true: subscription is healthy, button stays hidden
            }).catch(function () {});
          } else {
            // No local subscription — silently resubscribe
            clearServerSubscriptions().then(function () {
              subscribe(reg, null, function () {
                // Failed — show button as fallback
                wireReactivateBtn();
                if (reactivateBtn) reactivateBtn.style.display = '';
              });
            });
          }
        });

        wireReactivateBtn();
        return;
      }

      // Show enable button if permission not yet granted
      var btn = document.getElementById('push-enable-btn');
      if (btn) {
        btn.style.display = '';
        btn.addEventListener('click', function () {
          Notification.requestPermission().then(function (perm) {
            btn.style.display = 'none';
            if (perm === 'granted') subscribe(reg, null, null);
          });
        });
      }
    }).catch(function () {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPush);
  } else {
    initPush();
  }
})();
