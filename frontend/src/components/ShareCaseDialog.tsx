import { useEffect, useState } from 'react';
import { X, Copy, Download, QrCode, Check, ExternalLink } from 'lucide-react';
import * as QRCode from 'qrcode';
import { buildCaseShareUrl, copyShareUrl } from '../utils/share';
import './ShareCaseDialog.css';

interface ShareCaseDialogProps {
  caseId: string;
  isOpen: boolean;
  onClose: () => void;
}

export default function ShareCaseDialog({ caseId, isOpen, onClose }: ShareCaseDialogProps): JSX.Element | null {
  const [shareUrl, setShareUrl] = useState('');
  const [qrDataUrl, setQrDataUrl] = useState('');
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle');
  const [qrError, setQrError] = useState('');

  useEffect(() => {
    if (!isOpen) return undefined;
    const url = buildCaseShareUrl(caseId);
    setShareUrl(url);
    setCopyState('idle');
    setQrError('');
    let cancelled = false;
    void QRCode.toDataURL(url, {
      width: 320,
      margin: 2,
      errorCorrectionLevel: 'M',
      color: { dark: '#102a4c', light: '#ffffff' },
    }).then((dataUrl) => {
      if (!cancelled) setQrDataUrl(dataUrl);
    }).catch(() => {
      if (!cancelled) setQrError('二维码生成失败，请使用复制链接分享。');
    });
    return () => { cancelled = true; };
  }, [caseId, isOpen]);

  useEffect(() => {
    if (!isOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const handleCopy = async (): Promise<void> => {
    try {
      await copyShareUrl(shareUrl);
      setCopyState('copied');
      window.setTimeout(() => setCopyState('idle'), 2200);
    } catch {
      setCopyState('error');
    }
  };

  const handleDownload = (): void => {
    if (!qrDataUrl) return;
    const link = document.createElement('a');
    link.href = qrDataUrl;
    link.download = `crosscomply-${caseId.slice(-12)}-二维码.png`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  return (
    <div className="share-dialog__scrim" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="share-dialog" role="dialog" aria-modal="true" aria-labelledby="share-dialog-title">
        <header className="share-dialog__header">
          <div>
            <span className="share-dialog__eyebrow"><QrCode size={15} aria-hidden="true" />案件交接</span>
            <h2 id="share-dialog-title">分享案件</h2>
            <p>通过网页链接或二维码交给有权限的协作者查看。</p>
          </div>
          <button type="button" className="share-dialog__close" onClick={onClose} aria-label="关闭分享窗口"><X size={19} /></button>
        </header>

        <div className="share-dialog__body">
          <div className="share-dialog__qr-panel">
            <div className="share-dialog__qr-frame">
              {qrDataUrl ? <img src={qrDataUrl} alt="案件分享二维码" /> : <span className="share-dialog__qr-loading">正在生成二维码…</span>}
            </div>
            <span className="share-dialog__qr-hint">扫码后登录，按当前账号权限查看</span>
            {qrError ? <span className="share-dialog__error" role="alert">{qrError}</span> : null}
            <button type="button" className="share-dialog__download" onClick={handleDownload} disabled={!qrDataUrl}><Download size={16} aria-hidden="true" />下载二维码</button>
          </div>

          <div className="share-dialog__link-panel">
            <div className="share-dialog__section-label">案件网页链接</div>
            <div className="share-dialog__link-box"><span>{shareUrl}</span><a href={shareUrl} target="_blank" rel="noreferrer" aria-label="在新窗口打开案件链接"><ExternalLink size={15} /></a></div>
            <button type="button" className="share-dialog__copy" onClick={handleCopy}><span>{copyState === 'copied' ? <Check size={17} aria-hidden="true" /> : <Copy size={17} aria-hidden="true" />}</span>{copyState === 'copied' ? '已复制链接' : '复制案件链接'}</button>
            {copyState === 'error' ? <span className="share-dialog__error" role="alert">复制失败，请选中上方链接手动复制。</span> : null}
            <p className="share-dialog__security-note">链接不会包含材料正文；打开时仍会经过登录和案件权限校验。</p>
          </div>
        </div>
      </section>
    </div>
  );
}
