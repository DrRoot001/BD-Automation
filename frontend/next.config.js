/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || 'http://localhost:8002';

const nextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/api/:path*`,
      },
    ]
  },
  async headers() {
    return [
      {
        source: '/ws/:path*',
        headers: [{ key: 'Cache-Control', value: 'no-store' }],
      },
    ]
  },
}

module.exports = nextConfig
