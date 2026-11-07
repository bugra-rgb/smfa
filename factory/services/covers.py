"""Immutable files and isolated, revision-checked cover settings."""
from __future__ import annotations
import hashlib
import io
import os
import warnings
from pathlib import Path
from urllib.parse import urlsplit
from PIL import Image, ImageOps, ImageCms, UnidentifiedImageError
from fastapi import HTTPException
from sqlalchemy import select, update
from factory.config import settings
from factory.models import Account, new_id, utcnow
from factory.update_models import ReelsCoverAsset, ReelsCoverSetting, PublicationCover
from factory.providers.base import ProviderError

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 24_000_000

def instagram_account(session, account_id):
    account = session.get(Account, account_id)
    if not account or (account.settings or {}).get('managed_platform') not in (None, '', 'instagram'):
        raise HTTPException(404, 'Instagram hesabı bulunamadı.')
    return account

def image_bytes(raw: bytes, x=.5, y=.5, zoom=1):
    if not raw or len(raw) > MAX_BYTES:
        raise HTTPException(413, 'Görsel en fazla 8 MB olabilir.')
    if not all(isinstance(n, (float, int)) and not isinstance(n, bool) for n in (x,y,zoom)) or not (0 <= x <= 1 and 0 <= y <= 1 and 1 <= zoom <= 4):
        raise HTTPException(422, 'Kırpma seçimi geçersiz.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as check:
                if check.format not in {'JPEG','PNG','WEBP'} or check.width*check.height > MAX_PIXELS or min(check.size) < 32 or getattr(check, 'n_frames', 1) != 1:
                    raise ValueError()
                check.verify()
            with Image.open(io.BytesIO(raw)) as source:
                im = ImageOps.exif_transpose(source)
                im.load()
                icc = im.info.get('icc_profile')
                if icc:
                    # Bad/unsupported profiles fail closed instead of silently changing color.
                    im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile('sRGB'), outputMode='RGB')
                elif im.mode in ('RGBA', 'LA', 'P'):
                    layer = im.convert('RGBA')
                    im = Image.new('RGB', layer.size, 'white'); im.paste(layer, mask=layer.getchannel('A'))
                else:
                    im = im.convert('RGB')
                w,h=im.size
                crop_w=min(w,h*9/16)/zoom; crop_h=crop_w*16/9
                left=(w-crop_w)*x; top=(h-crop_h)*y
                im=im.crop((round(left),round(top),round(left+crop_w),round(top+crop_h))).resize((1080,1920),Image.Resampling.LANCZOS)
                out=io.BytesIO()
                for quality in (90,84,76,65):
                    out.seek(0);out.truncate();im.save(out,format='JPEG',quality=quality,optimize=True)
                    if out.tell() < 2*1024*1024: break
                if out.tell() >= 2*1024*1024: raise ValueError()
                return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError, ImageCms.PyCMSError):
        raise HTTPException(422, 'Görsel okunamadı. Geçerli, tek kare JPG, PNG veya WebP yükleyin.') from None

def asset_path(asset):
    root=(Path(settings.data_dir)/'reels-covers').resolve()
    path=(root/asset.storage_key).resolve()
    if path.parent != root or path.suffix != '.jpg':
        raise ProviderError('Kapak dosyasının yolu geçersiz.', 'cover_unavailable')
    return path

def create_asset(session, account_id, raw, x=.5, y=.5, zoom=1):
    instagram_account(session, account_id)
    encoded=image_bytes(raw,x,y,zoom)
    asset=ReelsCoverAsset(id=new_id(),account_id=account_id,storage_key=new_id()+'.jpg',checksum=hashlib.sha256(encoded).hexdigest(),bytes_count=len(encoded),width=1080,height=1920)
    path=asset_path(asset);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:
        f.write(encoded);f.flush();os.fsync(f.fileno())
    session.add(asset);session.flush()
    return asset

def asset_data(asset):
    return {'id':asset.id,'url':f'/api/reels-covers/{asset.id}.jpg','width':asset.width,'height':asset.height,'bytes':asset.bytes_count,'created_at':asset.created_at.isoformat()}

def get_setting(session, account_id):
    instagram_account(session,account_id)
    row=session.get(ReelsCoverSetting,account_id)
    asset=session.get(ReelsCoverAsset,row.asset_id) if row and row.asset_id else None
    return {'account_id':account_id,'enabled':bool(row and row.enabled),'revision':row.revision if row else 0,'asset':asset_data(asset) if asset else None,'updated_at':row.updated_at.isoformat() if row else None}

def patch_setting(session, account_id, values):
    instagram_account(session,account_id)
    # Serialize first insert without assigning to Account/settings or OAuthToken.
    session.scalar(select(Account.id).where(Account.id==account_id).with_for_update())
    row=session.get(ReelsCoverSetting,account_id,populate_existing=True)
    if not row:
        row=ReelsCoverSetting(account_id=account_id,revision=0,enabled=False)
        session.add(row);session.flush()
    changes={k:v for k,v in values.items() if k in ('enabled','asset_id')}
    selected=changes.get('asset_id',row.asset_id)
    if selected:
        asset=session.get(ReelsCoverAsset,selected)
        if not asset or asset.account_id != account_id:
            raise HTTPException(422,'Kapak bu hesaba ait değil.')
    if changes.get('enabled',row.enabled) and not selected:
        raise HTTPException(422,'Sabit kapak için önce görsel seçin.')
    result=session.execute(update(ReelsCoverSetting).where(ReelsCoverSetting.account_id==account_id,ReelsCoverSetting.revision==values['revision']).values(**changes,revision=ReelsCoverSetting.revision+1,updated_at=utcnow()))
    if result.rowcount != 1:
        raise HTTPException(409,'Kapak başka bir oturumda değişti. Yenileyip tekrar deneyin.')
    session.flush();session.expire_all()
    return get_setting(session,account_id)

def resolve_publication_cover(session, pub, checkpoint):
    """Shared path for manual/scheduled jobs. Caller owns the publication lease."""
    row=session.get(PublicationCover,pub.id)
    if not row and not settings.smfa_covers_enabled:
        return {}
    if not row:
        row=PublicationCover(publication_id=pub.id,mode='default',revision=0)
        session.add(row)
    if not row.frozen_at:
        if any(checkpoint.get(k) for k in ('create_started','container_id','publish_started','remote_id')):
            row.mode='legacy';row.asset_id=None
        elif row.mode=='default':
            default=session.get(ReelsCoverSetting,pub.account_id)
            row.asset_id=default.asset_id if settings.smfa_covers_enabled and default and default.enabled else None
        elif row.mode=='none':
            row.asset_id=None
        row.frozen_at=utcnow();session.commit()
    if not row.asset_id:
        return {}
    asset=session.get(ReelsCoverAsset,row.asset_id)
    if not asset or asset.account_id != pub.account_id:
        raise ProviderError('Sabitlenmiş kapak bulunamadı; yalnız bu yayın bekletildi.','cover_unavailable')
    path=asset_path(asset)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=asset.checksum:
        raise ProviderError('Kapak dosyası doğrulanamadı; yalnız bu yayın bekletildi.','cover_unavailable')
    origin=settings.media_base_url or settings.public_base_url
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query:
        raise ProviderError('Kapak için HTTPS medya adresi gerekli.','cover_unavailable')
    return {'cover_url':origin.rstrip('/')+f'/api/reels-covers/{asset.id}.jpg','cover_asset_id':asset.id}
