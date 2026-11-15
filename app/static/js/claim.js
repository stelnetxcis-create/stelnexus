/* Counter claim: camera QR scan (BarcodeDetector) with a typed / USB-scanner fallback. */
(function () {
  'use strict';
  const form = document.querySelector('[data-claim-form]');
  if (!form) return;
  const input = form.querySelector('input[name="code"]');
  const video = document.querySelector('[data-scan-video]');
  const button = document.querySelector('[data-scan-start]');
  const note = document.querySelector('[data-scan-note]');
  let stream = null;
  let timer = null;

  // A USB / Bluetooth scanner "types" the code and presses Enter: submit as soon as it arrives.
  if (input) input.focus({ preventScroll: true });

  function stop() {
    if (timer) clearInterval(timer);
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = timer = null;
    if (video) video.hidden = true;
    if (button) button.textContent = 'Scan with camera';
  }

  if (!button || !video) return;
  if (!('BarcodeDetector' in window) || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    button.hidden = true;
    if (note) note.textContent = 'Camera scanning is not supported in this browser. Type the code or use a barcode scanner.';
    return;
  }

  button.addEventListener('click', async () => {
    if (stream) { stop(); return; }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
    } catch (e) {
      if (note) note.textContent = 'Camera permission was denied. Type the ticket code instead.';
      return;
    }
    const detector = new BarcodeDetector({ formats: ['qr_code'] });
    video.srcObject = stream;
    video.hidden = false;
    await video.play().catch(() => {});
    button.textContent = 'Stop camera';
    timer = setInterval(async () => {
      try {
        const found = await detector.detect(video);
        if (found && found.length) {
          input.value = found[0].rawValue;
          stop();
          form.submit();
        }
      } catch (e) { /* frame not ready */ }
    }, 350);
  });
  window.addEventListener('pagehide', stop);
})();
