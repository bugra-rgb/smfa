"""Scoped caption delivery guard; never creates or retries a publication itself."""
import hashlib
from types import MethodType
from urllib.parse import parse_qs
from factory.providers.base import ProviderError

VERSION = 'caption-delivery-20261031.1'

def install(publisher, account, pub, video, metadata):
    if (account.settings or {}).get('verify_caption_delivery') is not True or pub.platform != 'instagram':
        return
    from factory.services.account_captions import config, provider_caption
    expected = config(account)['text']
    # The pipeline has already saved the exact copy before this boundary.
    caption = provider_caption(metadata, 'instagram')
    saved = (video.metadata_json or {}).get('instagram', {}).get('caption')
    if not expected or caption != expected or saved != expected:
        raise ProviderError('Kayitli aciklama ile gonderilecek metin eslesmiyor.', 'invalid_metadata', False)
    if type(publisher).__name__ != 'InstagramPublisher':
        raise ProviderError('Aciklama kontrolu icin yayin yolu dogrulanamadi.', 'invalid_metadata', False)
    original = publisher.publish
    digest = hashlib.sha256(caption.encode('utf-8')).hexdigest()

    def publish(self, video_path, md, credentials, checkpoint=None, save_checkpoint=None):
        proof = {}
        cp = dict(checkpoint or {})
        def guard(request):
            if request.method != 'POST' or not request.url.path.endswith('/media'):
                return
            actual = parse_qs(request.content.decode('utf-8'), keep_blank_values=True).get('caption', [])
            if actual != [caption]:
                raise ProviderError('Instagram isteginde aciklama eksik veya farkli.', 'invalid_metadata', False)
            proof.update(caption_delivery_version=VERSION, caption_sent_sha256=digest)
        def save(value):
            value.update(proof)
            if save_checkpoint:
                save_checkpoint(value)
        hooks = publisher.client.event_hooks.setdefault('request', [])
        hooks.append(guard)
        try:
            result = original(video_path, md, credentials, checkpoint=cp, save_checkpoint=save)
        finally:
            hooks.remove(guard)
        if proof:
            result['checkpoint'] = {**(result.get('checkpoint') or {}), **proof}
        # Keep remote success authoritative: caption read failure must never resend a video.
        if result.get('status') == 'PUBLISHED' and result.get('remote_id') and not cp.get('remote_id'):
            from factory.providers.base import bearer
            state = dict(result.get('checkpoint') or {})
            try:
                response = publisher.client.get(publisher.base+'/'+str(result['remote_id']), headers=bearer(credentials), params={'fields':'id,caption'})
                data = response.json() if response.status_code == 200 else {}
                state['caption_remote_check'] = ('verified' if data.get('caption') == caption else 'missing_or_changed') if response.status_code == 200 else 'unavailable'
            except Exception:
                state['caption_remote_check'] = 'unavailable'
            state['caption_expected_sha256'] = digest
            save(state)
            result['checkpoint'] = state
        return result
    publisher.publish = MethodType(publish, publisher)
