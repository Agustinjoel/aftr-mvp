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

  function subscribe(reg) {
    reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
    }).then(function (sub) {
      sendSubscriptionToServer(sub).catch(function () {});
    }).catch(function () {});
  }

  function initPush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;

    navigator.serviceWorker.register('/static/sw.js').then(function (reg) {
      // Check current permission state
      if (Notification.permission === 'granted') {
        reg.pushManager.getSubscription().then(function (existing) {
          if (!existing) subscribe(reg);
          else sendSubscriptionToServer(existing).catch(function () {});
        });
        return;
      }

      // Show our own prompt button instead of asking immediately
      var btn = document.getElementById('push-enable-btn');
      if (btn) {
        btn.style.display = '';
        btn.addEventListener('click', function () {
          Notification.requestPermission().then(function (perm) {
            btn.style.display = 'none';
            if (perm === 'granted') subscribe(reg);
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
