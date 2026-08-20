/**
 * Build the canonical in-app URL used to share a case.
 *
 * The case id is kept in the existing `?case=` deep link so that sharing does
 * not introduce a second routing scheme or expose any case data in the QR
 * code itself. Server-side authentication and authorization still apply when
 * the link is opened.
 */
export function buildCaseShareUrl(caseId: string): string {
  const url = new URL(window.location.href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('case', caseId);
  return url.toString();
}

/** Copy text with a small legacy fallback for browsers without Clipboard API. */
export async function copyShareUrl(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!copied) throw new Error('当前浏览器不支持复制链接，请手动复制。');
}
