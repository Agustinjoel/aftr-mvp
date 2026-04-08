(function () {
  'use strict';

  var VAPID_PUBLIC_KEY = 'BFChaN7xYNXVMuUdUvyinJ1O_HLsI5AD7GaMu1syhumy0Sv-pCnQss7W1wy5wnWZJurpSK4M1fOmna3jYAqB99c';

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

  function isStaleEndpoint(endpoint) {
    // Formato viejo de FCM (apagado por Google en junio 2024)
    return endpoint && endpoint.indexOf('fcm.googleapis.com/fcm/send/') !== -1;
  }

  function initPush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;

    navigator.serviceWorker.register('/sw.js').then(function (reg) {
      var reactivateBtn = document.getElementById('push-reactivate-btn');

      if (Notification.permission === 'granted') {
        reg.pushManager.getSubscription().then(function (existing) {
          if (existing && isStaleEndpoint(existing.endpoint)) {
            // Endpoint viejo (FCM legacy apagado) — forzar re-suscripción
            existing.unsubscribe().then(function () {
              subscribe(reg, null, null);
            }).catch(function () {
              subscribe(reg, null, null);
            });
            return;
          }
          if (existing) {
            sendSubscriptionToServer(existing).catch(function () {});
          } else {
            subscribe(reg, null, null);
          }
        });

        // Show reactivate button so user can force re-register if needed
        if (reactivateBtn) {
          reactivateBtn.style.display = '';
          reactivateBtn.addEventListener('click', function () {
            reactivateBtn.disabled = true;
            reactivateBtn.textContent = '⏳ Activando...';
            // Unsubscribe first to force fresh subscription
            reg.pushManager.getSubscription().then(function (existing) {
              var doSubscribe = function () {
                subscribe(reg, function () {
                  reactivateBtn.textContent = '✅ Activadas';
                  setTimeout(function () {
                    reactivateBtn.style.display = 'none';
                  }, 3000);
                }, function (err) {
                  reactivateBtn.disabled = false;
                  var msg = (err && err.message) ? err.message : String(err);
                  reactivateBtn.textContent = '❌ ' + msg;
                  console.error('[AFTR push] subscribe error:', err);
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
