export const BOAT_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 48 48">
  <g stroke-linejoin="round">
    <path fill="#f8fafc" stroke="#475569" stroke-width="1.5" d="M24 5 L31 26 L29 40 L19 40 L17 26 Z"/>
    <path fill="#cbd5e1" stroke="#64748b" stroke-width="1" d="M24 12 L24 34 L30 28 Z"/>
    <path fill="#e2e8f0" stroke="#64748b" stroke-width="1" d="M24 12 L24 34 L18 28 Z"/>
    <circle cx="24" cy="20" r="1.5" fill="#64748b" stroke="none"/>
  </g>
</svg>`;

/** North-pointing wind arrow (rotate via icon-rotate = flow-toward bearing). */
export const WIND_ARROW_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <path fill="#ffffff" stroke="#64748b" stroke-width="1.5" stroke-linejoin="round"
    d="M24 1 L30 16 L27 16 L27 46 L21 46 L21 16 L18 16 Z"/>
</svg>`;

export function svgToImage(svg: string, size = 48): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image(size, size);
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
}
