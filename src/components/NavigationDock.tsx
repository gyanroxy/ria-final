'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import Image from 'next/image';
import { 
  Volume2, 
  VolumeX, 
  Maximize, 
  Minimize, 
  ChevronDown, 
  Sparkles, 
  Menu, 
  X, 
  Home, 
  Info, 
  Wrench, 
  MessageSquare,
  PhoneCall,
  ArrowUpRight,
  HelpCircle
} from 'lucide-react';

interface NavigationDockProps {
  activeSection: number;
  onNavigate: (sectionIndex: number) => void;
  isAudioMuted: boolean;
  onToggleAudio: () => void;
}

export default function NavigationDock({
  activeSection,
  onNavigate,
  isAudioMuted,
  onToggleAudio,
}: NavigationDockProps) {
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);

  const sections = [
    { label: '01', name: 'Home' },
    { label: '02', name: 'Capabilities' },
    { label: '03', name: 'Synergy' },
    { label: '04', name: 'Contact' },
  ];

  const mobileNavItems = [
    { label: 'Home', href: '/', icon: Home, subtitle: 'Autonomous Collection Core', isSection: true },
    { label: 'About', href: '/about', icon: Info, subtitle: 'Latency & Architecture', isSection: false },
    { label: 'Services', href: '/services', icon: Wrench, subtitle: 'Enterprise Editions & Telemetry', isSection: false },
    { label: 'Contact', href: '/contact', icon: MessageSquare, subtitle: 'Direct Desk +91 8106668552', isSection: false },
    { label: 'Live Demo', href: '/demo', icon: PhoneCall, subtitle: 'Live Voice Agent Calling', isSection: false },
    { label: 'Q/A s', href: '/qa', icon: HelpCircle, subtitle: '60 Platform Questions & Answers', isSection: false },
  ];

  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
      setIsFullscreen(true);
    } else {
      if (document.exitFullscreen) {
        document.exitFullscreen().catch(() => {});
        setIsFullscreen(false);
      }
    }
  };

  useEffect(() => {
    const handleFsChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener('fullscreenchange', handleFsChange);
    return () => document.removeEventListener('fullscreenchange', handleFsChange);
  }, []);

  // Lock body scroll when mobile menu is open
  useEffect(() => {
    if (isMobileMenuOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isMobileMenuOpen]);

  return (
    <>
      {/* Top Header Navigation */}
      <header className="site-header">
        {/* Brand & Logo Plate with Exact Square Logo */}
        <Link
          href="/"
          onClick={(e) => {
            if (activeSection !== 0) {
              e.preventDefault();
              onNavigate(0);
            }
          }}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.85rem',
            pointerEvents: 'auto',
            textDecoration: 'none',
          }}
        >
          {/* Exact Square Logo */}
          <div
            style={{
              position: 'relative',
              width: '46px',
              height: '46px',
              borderRadius: '8px',
              overflow: 'hidden',
              border: '1.5px solid rgba(255, 255, 255, 0.4)',
              boxShadow: '0 4px 14px rgba(0, 0, 0, 0.95)',
              backgroundColor: '#FFFFFF',
              flexShrink: 0,
            }}
          >
            <Image
              src="/images/ria-logo.jpg"
              alt="RIA ai Agent Logo"
              fill
              style={{ objectFit: 'contain' }}
              priority
            />
          </div>
          <div>
            <div
              style={{
                fontFamily: 'var(--font-mono)',
                fontWeight: 800,
                fontSize: '0.92rem',
                letterSpacing: '0.1em',
                color: '#FFFFFF',
                textTransform: 'uppercase',
                lineHeight: 1.2,
              }}
            >
              RIA <span style={{ color: '#A3A3A3' }}>//</span> AI AGENT
            </div>
            <div
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: '0.64rem',
                color: '#A3A3A3',
                letterSpacing: '0.08em',
                lineHeight: 1.2,
              }}
            >
              AUTONOMOUS FLEET 3.0
            </div>
          </div>
        </Link>

        {/* Central Navbar: Desktop View */}
        <nav
          aria-label="Main Navigation"
          className="desktop-nav-only"
          style={{
            alignItems: 'center',
            gap: '0.35rem',
            background: 'linear-gradient(180deg, rgba(26, 26, 30, 0.9) 0%, rgba(12, 12, 14, 0.96) 100%)',
            padding: '0.35rem 0.55rem',
            borderRadius: '9999px',
            border: '1px solid rgba(255, 255, 255, 0.18)',
            boxShadow: '0 8px 24px rgba(0, 0, 0, 0.85), inset 0 1px 1px rgba(255, 255, 255, 0.25)',
            backdropFilter: 'blur(16px)',
            pointerEvents: 'auto',
          }}
        >
          {/* Home */}
          <button
            onClick={() => onNavigate(0)}
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: activeSection === 0
                ? 'linear-gradient(180deg, #FFFFFF 0%, #D4D4D4 100%)'
                : 'transparent',
              color: activeSection === 0 ? '#000000' : 'var(--white-muted)',
              fontWeight: activeSection === 0 ? 800 : 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: activeSection === 0 ? '1px solid #FFFFFF' : '1px solid transparent',
              boxShadow: activeSection === 0
                ? '0 2px 8px rgba(255, 255, 255, 0.35), inset 0 1px 1px #FFFFFF'
                : 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
            }}
          >
            Home
          </button>

          {/* About (Separate Page) */}
          <Link
            href="/about"
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: 'transparent',
              color: 'var(--white-muted)',
              fontWeight: 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: '1px solid transparent',
              textDecoration: 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
              display: 'inline-block',
            }}
          >
            About
          </Link>

          {/* Services (Separate Page) */}
          <Link
            href="/services"
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: 'transparent',
              color: 'var(--white-muted)',
              fontWeight: 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: '1px solid transparent',
              textDecoration: 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
              display: 'inline-block',
            }}
          >
            Services
          </Link>

          {/* Contact (Dedicated /contact page) */}
          <Link
            href="/contact"
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: 'transparent',
              color: 'var(--white-muted)',
              fontWeight: 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: '1px solid transparent',
              textDecoration: 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
              display: 'inline-block',
            }}
          >
            Contact
          </Link>

          {/* Live Demo (Voice Calling Project) */}
          <Link
            href="/demo"
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: 'transparent',
              color: 'var(--white-muted)',
              fontWeight: 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: '1px solid transparent',
              textDecoration: 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
              display: 'inline-block',
            }}
          >
            Live Demo
          </Link>

          {/* Q/A s (Separate Dedicated FAQ Page) */}
          <Link
            href="/qa"
            style={{
              padding: '0.45rem 1.1rem',
              borderRadius: '9999px',
              background: 'transparent',
              color: 'var(--white-muted)',
              fontWeight: 600,
              fontSize: '0.82rem',
              fontFamily: 'var(--font-mono)',
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
              border: '1px solid transparent',
              textDecoration: 'none',
              cursor: 'pointer',
              transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
              display: 'inline-block',
            }}
          >
            Q/A s
          </Link>
        </nav>

        {/* Right Header Status & Controls */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.65rem',
            pointerEvents: 'auto',
          }}
        >
          {/* Audio Button */}
          <button
            onClick={onToggleAudio}
            aria-label={isAudioMuted ? 'Unmute Audio' : 'Mute Audio'}
            title={isAudioMuted ? 'Unmute Audio' : 'Mute Audio'}
            style={{
              width: '38px',
              height: '38px',
              borderRadius: '50%',
              background: 'linear-gradient(145deg, #222228 0%, #0d0d10 100%)',
              border: '1px solid rgba(255, 255, 255, 0.25)',
              boxShadow: '0 4px 10px rgba(0,0,0,0.8), inset 0 1px 1px rgba(255, 255, 255, 0.35), inset 0 -2px 4px #000000',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: isAudioMuted ? 'rgba(255, 255, 255, 0.35)' : '#FFFFFF',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
            }}
          >
            {isAudioMuted ? <VolumeX size={17} /> : <Volume2 size={17} />}
          </button>

          {/* Fullscreen Button (Desktop Only) */}
          <button
            onClick={toggleFullscreen}
            aria-label="Toggle Fullscreen"
            title="Toggle Fullscreen"
            className="desktop-nav-only"
            style={{
              width: '38px',
              height: '38px',
              borderRadius: '50%',
              background: 'linear-gradient(145deg, #222228 0%, #0d0d10 100%)',
              border: '1px solid rgba(255, 255, 255, 0.25)',
              boxShadow: '0 4px 10px rgba(0,0,0,0.8), inset 0 1px 1px rgba(255, 255, 255, 0.35), inset 0 -2px 4px #000000',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#FFFFFF',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
            }}
          >
            {isFullscreen ? <Minimize size={17} /> : <Maximize size={17} />}
          </button>

          {/* Mobile Hamburger Toggle Button */}
          <button
            onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
            aria-label={isMobileMenuOpen ? 'Close Navigation Menu' : 'Open Navigation Menu'}
            className="mobile-nav-toggle"
            style={{
              width: '38px',
              height: '38px',
              borderRadius: '10px',
              background: isMobileMenuOpen 
                ? 'linear-gradient(145deg, #FFFFFF 0%, #CCCCCC 100%)' 
                : 'linear-gradient(145deg, #222228 0%, #0d0d10 100%)',
              border: isMobileMenuOpen ? '1px solid #FFFFFF' : '1px solid rgba(255, 255, 255, 0.3)',
              boxShadow: '0 4px 10px rgba(0,0,0,0.8), inset 0 1px 1px rgba(255, 255, 255, 0.35)',
              alignItems: 'center',
              justifyContent: 'center',
              color: isMobileMenuOpen ? '#000000' : '#FFFFFF',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
            }}
          >
            {isMobileMenuOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
      </header>

      {/* Fullscreen Mobile Navigation Drawer */}
      {isMobileMenuOpen && (
        <div 
          className="mobile-nav-overlay"
          role="dialog"
          aria-modal="true"
        >
          {/* Top Bar inside Drawer */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              borderBottom: '1px solid rgba(255, 255, 255, 0.12)',
              paddingBottom: '1rem',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
              <div
                style={{
                  position: 'relative',
                  width: '32px',
                  height: '32px',
                  borderRadius: '30px',
                  overflow: 'hidden',
                  border: '1px solid #FFFFFF',
                  backgroundColor: '#FFFFFF',
                  flexShrink: 0,
                }}
              >
                <Image
                  src="/images/ria-logo.jpg"
                  alt="RIA ai Agent Logo"
                  fill
                  style={{ objectFit: 'contain' }}
                />
              </div>
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: '0.82rem',
                  fontWeight: 800,
                  letterSpacing: '0.08em',
                  color: '#FFFFFF',
                }}
              >
                RIA AI AGENT
              </span>
            </div>

            <button
              onClick={() => setIsMobileMenuOpen(false)}
              style={{
                width: '38px',
                height: '38px',
                borderRadius: '10px',
                background: 'rgba(255, 255, 255, 0.08)',
                border: '1px solid rgba(255, 255, 255, 0.25)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#FFFFFF',
                cursor: 'pointer',
              }}
              aria-label="Close menu"
            >
              <X size={20} />
            </button>
          </div>

          {/* Navigation Links */}
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: '0.85rem',
              margin: '1.5rem 0',
            }}
          >
            {mobileNavItems.map((item) => {
              const Icon = item.icon;
              const isActive = item.isSection ? activeSection === 0 : false;

              if (item.isSection) {
                return (
                  <button
                    key={item.label}
                    onClick={() => {
                      onNavigate(0);
                      setIsMobileMenuOpen(false);
                    }}
                    className={`mobile-nav-item ${isActive ? 'active' : ''}`}
                    style={{ textAlign: 'left', width: '100%', cursor: 'pointer' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem' }}>
                      <div
                        style={{
                          padding: '0.5rem',
                          borderRadius: '8px',
                          background: isActive ? 'rgba(0, 0, 0, 0.15)' : 'rgba(255, 255, 255, 0.06)',
                        }}
                      >
                        <Icon size={20} color={isActive ? '#000000' : '#FFFFFF'} />
                      </div>
                      <div>
                        <div style={{ fontWeight: 800, fontSize: '1.05rem' }}>
                          {item.label}
                        </div>
                        <div
                          style={{
                            fontSize: '0.72rem',
                            fontFamily: 'var(--font-mono)',
                            color: isActive ? '#333333' : 'var(--white-muted)',
                            textTransform: 'none',
                            marginTop: '0.1rem',
                          }}
                        >
                          {item.subtitle}
                        </div>
                      </div>
                    </div>

                    <ArrowUpRight size={18} color={isActive ? '#000000' : 'rgba(255, 255, 255, 0.4)'} />
                  </button>
                );
              }

              return (
                <Link
                  key={item.label}
                  href={item.href}
                  onClick={() => setIsMobileMenuOpen(false)}
                  className="mobile-nav-item"
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem' }}>
                    <div
                      style={{
                        padding: '0.5rem',
                        borderRadius: '8px',
                        background: 'rgba(255, 255, 255, 0.06)',
                      }}
                    >
                      <Icon size={20} color="#FFFFFF" />
                    </div>
                    <div>
                      <div style={{ fontWeight: 800, fontSize: '1.05rem' }}>
                        {item.label}
                      </div>
                      <div
                        style={{
                          fontSize: '0.72rem',
                          fontFamily: 'var(--font-mono)',
                          color: 'var(--white-muted)',
                          textTransform: 'none',
                          marginTop: '0.1rem',
                        }}
                      >
                        {item.subtitle}
                      </div>
                    </div>
                  </div>

                  <ArrowUpRight size={18} color="rgba(255, 255, 255, 0.4)" />
                </Link>
              );
            })}
          </div>

          {/* Drawer Bottom Quick Action */}
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: '0.85rem',
              borderTop: '1px solid rgba(255, 255, 255, 0.12)',
              paddingTop: '1.25rem',
            }}
          >
            <a
              href="https://api.whatsapp.com/send?phone=918106668552&text=Hello%20RIA%20Intelligence%2C%20I%20would%20like%20to%20inquire%20about%20the%20Autonomous%20AI%20Collection%20Agents."
              target="_blank"
              rel="noopener noreferrer"
              className="btn-skeuo-primary"
              style={{
                textDecoration: 'none',
                width: '100%',
                padding: '0.9rem 1rem',
                fontSize: '0.88rem',
              }}
            >
              <MessageSquare size={17} color="#000000" />
              <span>Direct WhatsApp Desk (+91 8106668552)</span>
            </a>

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                fontFamily: 'var(--font-mono)',
                fontSize: '0.74rem',
                color: 'var(--white-muted)',
                padding: '0.35rem 0.25rem',
              }}
            >
              <span>STATUS: ONLINE</span>
              <span>RIA AI AGENT v3.0</span>
            </div>
          </div>
        </div>
      )}

      {/* Floating Monochromatic Section Dock (Right Side) */}
      <nav
        aria-label="Page navigation"
        className="mobile-dock-adjust"
        style={{
          position: 'fixed',
          right: '2.5rem',
          top: '50%',
          transform: 'translateY(-50%)',
          zIndex: 90,
          display: 'flex',
          flexDirection: 'column',
          gap: '1.25rem',
          background: 'linear-gradient(180deg, #1c1c20 0%, #0c0c0e 100%)',
          padding: '1.3rem 0.85rem',
          borderRadius: '24px',
          border: '1px solid rgba(255, 255, 255, 0.2)',
          boxShadow: '0 16px 40px rgba(0, 0, 0, 0.95), 0 1px 0 rgba(255, 255, 255, 0.3), inset 0 1px 2px rgba(255, 255, 255, 0.3), inset 0 -2px 5px #000000',
        }}
      >
        {sections.map((section, idx) => {
          const isActive = activeSection === idx;
          return (
            <button
              key={section.label}
              onClick={() => onNavigate(idx)}
              title={section.name}
              style={{
                position: 'relative',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: '34px',
                height: '34px',
                borderRadius: '50%',
                background: isActive
                  ? 'linear-gradient(180deg, #FFFFFF 0%, #D4D4D4 60%, #999999 100%)'
                  : 'linear-gradient(180deg, #09090b 0%, #17171c 100%)',
                border: isActive ? '1px solid #FFFFFF' : '1px solid rgba(255, 255, 255, 0.15)',
                color: isActive ? '#000000' : '#A3A3A3',
                fontFamily: 'var(--font-mono)',
                fontSize: '0.74rem',
                fontWeight: 800,
                cursor: 'pointer',
                transition: 'all 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
                boxShadow: isActive
                  ? '0 0 16px rgba(255, 255, 255, 0.8), 0 3px 6px rgba(0,0,0,0.8), inset 0 1px 2px #FFFFFF'
                  : 'inset 0 2px 5px rgba(0,0,0,0.8), 0 1px 0 rgba(255, 255, 255, 0.1)',
                transform: isActive ? 'scale(1.15)' : 'scale(1)',
              }}
            >
              {section.label}
            </button>
          );
        })}
      </nav>

      {/* Floating Bottom Quick Next Indicator */}
      {activeSection < 3 && (
        <div
          className={activeSection === 0 ? 'scroll-next-indicator on-hero' : 'scroll-next-indicator'}
          onClick={() => onNavigate(activeSection + 1)}
          style={{
            position: 'fixed',
            bottom: 'max(1.5rem, env(safe-area-inset-bottom))',
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 90,
            cursor: 'pointer',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '0.35rem',
            color: '#FFFFFF',
            transition: 'all 0.3s ease',
          }}
        >
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: '0.68rem',
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              textShadow: '0 2px 6px rgba(0, 0, 0, 0.95)',
            }}
          >
            SCROLL TO EXPLORE
          </span>
          <div
            style={{
              animation: 'float-slow 2s infinite ease-in-out',
            }}
          >
            <ChevronDown size={20} color="#FFFFFF" />
          </div>
        </div>
      )}
    </>
  );
}
