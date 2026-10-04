'use client';

import React, { useRef, useEffect } from 'react';
import dynamic from 'next/dynamic';
import { ArrowRight, Cpu } from 'lucide-react';

// WebRTC/microphone code is browser-only
const LiveDemoCard = dynamic(() => import('@/components/livedemo/LiveDemoCard'), { ssr: false });

interface HeroSectionProps {
  onExploreClick: () => void;
  isMuted: boolean;
  isActive?: boolean;
}

export default function HeroSection({ onExploreClick, isMuted, isActive = true }: HeroSectionProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.defaultMuted = isMuted;
      videoRef.current.muted = isMuted;
    }
  }, [isMuted]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    if (isActive) {
      const playPromise = video.play();
      if (playPromise !== undefined) {
        playPromise.catch(() => {
          // If browser blocked autoplay, ensure muted and retry
          video.muted = true;
          video.play().catch(() => {});
        });
      }
    }
  }, [isActive]);

  return (
    <section id="section-0" className="page-section">
      {/* 1st Video Background - Clean & Bright with Zero Shadows */}
      <div className="video-bg-layer">
        <video
          ref={videoRef}
          src="/videos/1st-video.mp4"
          autoPlay
          loop
          muted={isMuted}
          playsInline
          preload="auto"
          controls={false}
          aria-hidden="true"
          onLoadedData={() => {
            if (videoRef.current && isActive) {
              videoRef.current.play().catch(() => {});
            }
          }}
        />
        {/* Zero shadow overlays on front home page */}
      </div>

      {/* Hero Content */}
      <div className="section-content hero-with-demo">
        <div
          className="hero-copy"
          style={{
            maxWidth: '760px',
            display: 'flex',
            flexDirection: 'column',
            gap: '1.75rem',
          }}
        >
          {/* User Required Text: Advanced AI collection\nAgent in white font */}
          <div style={{ perspective: '1000px' }}>
            <h1
              style={{
                fontSize: 'clamp(2.1rem, 7.5vw, 5.2rem)',
                fontWeight: 800,
                lineHeight: 1.08,
                letterSpacing: '-0.03em',
                color: '#FFFFFF',
                textTransform: 'none',
                whiteSpace: 'pre-line',
              }}
            >
              Advanced AI collection{'\n'}
              <span
                style={{
                  color: '#FFFFFF',
                  display: 'inline-block',
                }}
              >
                Agent
              </span>
            </h1>
          </div>

          {/* Subtitle / Description */}
          <p
            style={{
              fontSize: 'clamp(0.98rem, 1.6vw, 1.3rem)',
              color: 'var(--white-dim)',
              lineHeight: 1.6,
              maxWidth: '680px',
              fontWeight: 400,
            }}
          >
            Autonomous conversational intelligence engineered for high-concurrency customer operations, real-time context reasoning, and instant tactile resolution.
          </p>

          {/* Action Buttons */}
          <div
            className="responsive-hero-actions"
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: '1rem',
              paddingTop: '0.75rem',
            }}
          >
            <button
              onClick={onExploreClick}
              className="btn-skeuo-primary"
              id="hero-explore-btn"
            >
              <span>Deploy Autonomous Fleet</span>
              <ArrowRight size={18} color="#000000" />
            </button>

            <button
              onClick={onExploreClick}
              className="btn-skeuo-secondary"
              id="hero-architecture-btn"
            >
              <Cpu size={18} color="#FFFFFF" />
              <span>System Architecture</span>
            </button>
          </div>
        </div>

        <LiveDemoCard />
      </div>
    </section>
  );
}
