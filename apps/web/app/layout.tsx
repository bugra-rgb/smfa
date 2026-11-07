import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = { title: 'SMF · Social Media Fabrica', applicationName: 'SMF · Social Media Fabrica', description: 'SMF kontrol merkezi ile hesaplarınızı, içerik üretiminizi ve yayınlarınızı tek yerden yönetin.' };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="tr"><body>{children}</body></html>; }
