import type { NextConfig } from 'next';
const netlifyExport = process.env.SMF_NETLIFY_EXPORT === 'true';
const config: NextConfig = {
  output: netlifyExport ? 'export' : 'standalone',
  poweredByHeader: false,
  // Static exports use Netlify's CDN rules; Docker keeps the same-origin proxy.
  ...(!netlifyExport ? {
    async rewrites() { return [{ source: '/api/:path*', destination: `${process.env.API_INTERNAL_URL || 'http://backend:8000'}/api/:path*` }]; },
    async headers() { return [{ source: '/:path*', headers: [
      { key: 'X-Content-Type-Options', value: 'nosniff' },
      { key: 'X-Frame-Options', value: 'DENY' },
      { key: 'Referrer-Policy', value: 'same-origin' },
      { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
    ] }]; },
  } : {}),
};
export default config;
