import { api } from './api';

export async function getVapidPublicKey(): Promise<string | null> {
  try {
    const res = await api.get<{ vapid_public_key: string }>('/api/notifications/vapid-public-key');
    return res.vapid_public_key;
  } catch {
    return null;
  }
}

export async function subscribeToPush(): Promise<{ ok: boolean; error?: string }> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    return { ok: false, error: 'Push notifications not supported in this browser' };
  }

  const vapidKey = await getVapidPublicKey();
  if (!vapidKey) {
    return { ok: false, error: 'Push notifications not configured on server' };
  }

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    return { ok: false, error: 'Notification permission denied' };
  }

  try {
    const registration = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('SW registration timeout')), 10_000)),
    ]);

    let subscription: PushSubscription;
    try {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });
    } catch (firstErr) {
      // Only retry with unsubscribe for VAPID key mismatch errors
      const isKeyMismatch = firstErr instanceof DOMException &&
        (firstErr.name === 'InvalidAccessError' || firstErr.name === 'InvalidStateError');
      if (!isKeyMismatch) throw firstErr;

      // VAPID key changed — clear stale subscription and retry
      const existing = await registration.pushManager.getSubscription();
      if (existing) await existing.unsubscribe();
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      });
    }

    const sub = subscription.toJSON();
    try {
      await api.post('/api/notifications/subscribe', {
        endpoint: sub.endpoint,
        p256dh_key: sub.keys?.p256dh,
        auth_key: sub.keys?.auth,
      });
    } catch (postErr) {
      // Server registration failed — roll back local subscription to stay in sync
      try {
        await subscription.unsubscribe();
      } catch {
        // Best-effort cleanup
      }
      throw postErr;
    }
    return { ok: true };
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'Unknown error';
    console.error('Push subscription failed:', msg);
    if (msg.includes('push service') || msg.includes('Registration failed')) {
      return {
        ok: false,
        error: 'Push service unavailable. If using Brave, go to Settings → Privacy → enable "Use Google services for push messaging".',
      };
    }
    return { ok: false, error: msg };
  }
}

export async function unsubscribeFromPush(): Promise<boolean> {
  try {
    const registration = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('SW registration timeout')), 10_000)),
    ]);
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return true;

    // Try server unsubscribe but don't let failure prevent local cleanup
    try {
      await api.post('/api/notifications/unsubscribe', {
        endpoint: subscription.endpoint,
      });
    } catch {
      // Server record may already be gone — that's fine
    }

    // Always clear the local subscription
    await subscription.unsubscribe();
    return true;
  } catch {
    return false;
  }
}

export async function getPushSubscriptionState(): Promise<'granted' | 'denied' | 'default' | 'unsupported'> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return 'unsupported';
  return Notification.permission as 'granted' | 'denied' | 'default';
}

export async function hasActivePushSubscription(): Promise<boolean> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return false;
  if (Notification.permission !== 'granted') return false;
  try {
    const reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('SW registration timeout')), 10_000),
      ),
    ]);
    const sub = await reg.pushManager.getSubscription();
    return sub !== null;
  } catch {
    return false;
  }
}

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}
