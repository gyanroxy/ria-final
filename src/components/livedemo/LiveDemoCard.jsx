'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
  BarVisualizer,
  useVoiceAssistant,
  useLocalParticipant,
  useRoomContext,
} from '@livekit/components-react';
import { ArrowRight, ChevronLeft, Mic, MicOff, PhoneOff } from 'lucide-react';
import {
  DAILY_LIMIT,
  LIVEKIT_URL,
  MIC_OPTIONS,
  callsUsedToday,
  fetchDemoToken,
  formatDuration,
  loadVisitor,
  roomErrorHandlers,
  saveVisitor,
  useCallGuards,
  useHangUp,
  validateVisitor,
} from '@/components/calling/demoSession';
import './LiveDemoCard.css';

export default function LiveDemoCard() {
  // idle → form → connecting → live → ended | limit | error
  const [stage, setStage] = useState('idle');
  const [used, setUsed] = useState(0);
  const [visitor, setVisitor] = useState(null);
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [formError, setFormError] = useState('');
  const [token, setToken] = useState(null);
  const [notice, setNotice] = useState('');
  const connectedAt = useRef(0);

  useEffect(() => {
    setUsed(callsUsedToday());
    const saved = loadVisitor();
    if (saved) {
      setVisitor(saved);
      setName(saved.name);
      setPhone(saved.phone);
    }
  }, []);

  const remaining = DAILY_LIMIT ? Math.max(0, DAILY_LIMIT - used) : Infinity;

  const startCall = useCallback(async (who) => {
    if (DAILY_LIMIT && callsUsedToday() >= DAILY_LIMIT) {
      setStage('limit');
      return;
    }
    setStage('connecting');
    setNotice('');
    connectedAt.current = 0;
    try {
      const { token: jwt, used: count } = await fetchDemoToken(who);
      setUsed(count);
      setToken(jwt);
      setStage('live');
    } catch (err) {
      setNotice(err?.message || 'Could not start the call. Please try again.');
      setStage('error');
    }
  }, []);

  const handleTap = () => {
    if (remaining === 0) return setStage('limit');
    if (visitor) return startCall(visitor);
    setStage('form');
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    const { visitor: who, error } = validateVisitor(name, phone);
    if (error) return setFormError(error);
    setFormError('');
    saveVisitor(who);
    setVisitor(who);
    startCall(who);
  };

  const endCall = useCallback((message = '') => {
    setToken(null);
    // the room's own disconnect event fires after a specific reason (mic blocked,
    // time cap); keep that reason instead of clearing it
    setNotice((prev) => message || prev);
    setStage('ended');
  }, []);

  return (
    <div className="ld-card" aria-live="polite">
      <div className="ld-card-head">
        <span className="ld-label">
          <span className={`ld-dot ${stage === 'live' ? 'is-live' : ''}`} />
          RIA · LIVE DEMO
        </span>
        {stage === 'live' ? (
          <span className="ld-pill ld-pill-live">LIVE</span>
        ) : (
          DAILY_LIMIT > 0 && (
            <span className="ld-pill">
              {remaining} free {remaining === 1 ? 'call' : 'calls'} today
            </span>
          )
        )}
      </div>

      <div className="ld-body">
        {stage === 'idle' && (
          <>
            <button type="button" className="ld-orb ld-orb-idle" onClick={handleTap} aria-label="Tap to talk to RIA">
              <span className="ld-orb-ring" />
              <span className="ld-orb-ring ld-orb-ring-2" />
              <Mic size={34} />
            </button>
            <p className="ld-title">Tap to talk</p>
            <p className="ld-sub">Try RIA live · no signup</p>
          </>
        )}

        {stage === 'form' && (
          <form className="ld-form" onSubmit={handleSubmit} noValidate>
            <button type="button" className="ld-back" onClick={() => setStage('idle')}>
              <ChevronLeft size={16} /> Back
            </button>
            <p className="ld-eyebrow">One quick step</p>
            <h3 className="ld-form-title">Talk to RIA live</h3>
            <p className="ld-form-sub">
              Tell us who&apos;s calling so RIA can greet you and our team can follow up. We&apos;ll only ask once.
            </p>
            <input
              className="ld-input"
              type="text"
              placeholder="Your name"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setFormError('');
              }}
              maxLength={60}
              autoComplete="name"
              autoFocus
            />
            <div className="ld-phone">
              <span className="ld-phone-prefix">🇮🇳 +91</span>
              <input
                className="ld-input ld-input-phone"
                type="tel"
                inputMode="numeric"
                placeholder="Mobile number"
                value={phone}
                onChange={(e) => {
                  setPhone(e.target.value);
                  setFormError('');
                }}
                maxLength={16}
                autoComplete="tel-national"
              />
            </div>
            {formError && <p className="ld-error">{formError}</p>}
            <button type="submit" className="ld-start">
              Start the call <ArrowRight size={18} />
            </button>
            <p className="ld-fine">Your browser will ask for microphone access. Calls are capped at 5 minutes.</p>
          </form>
        )}

        {stage === 'connecting' && (
          <>
            <div className="ld-orb ld-orb-connecting">
              <span className="ld-spinner" />
            </div>
            <p className="ld-title">Connecting to RIA…</p>
            <p className="ld-sub">Allow microphone access when asked</p>
          </>
        )}

        {stage === 'live' && token && (
          <LiveKitRoom
            className="ld-room"
            serverUrl={LIVEKIT_URL}
            token={token}
            connect
            video={false}
            audio={MIC_OPTIONS}
            onDisconnected={() => endCall()}
            {...roomErrorHandlers(connectedAt, endCall)}
          >
            <RoomAudioRenderer />
            <LiveCall visitorName={visitor?.name} onEnd={endCall} />
          </LiveKitRoom>
        )}

        {stage === 'ended' && (
          <>
            <p className="ld-title">{notice ? 'Call ended' : 'Thanks for talking to RIA'}</p>
            <p className="ld-sub">{notice || 'Our team will reach out to set up your free trial.'}</p>
            <button type="button" className="ld-start ld-start-compact" onClick={handleTap} disabled={remaining === 0}>
              {remaining === 0 ? 'No calls left today' : 'Talk again'} <ArrowRight size={18} />
            </button>
          </>
        )}

        {stage === 'error' && (
          <>
            <p className="ld-title">Couldn&apos;t connect</p>
            <p className="ld-sub">{notice}</p>
            <button type="button" className="ld-start ld-start-compact" onClick={() => setStage(visitor ? 'idle' : 'form')}>
              Try again <ArrowRight size={18} />
            </button>
          </>
        )}

        {stage === 'limit' && (
          <>
            <p className="ld-title">Daily limit reached</p>
            <p className="ld-sub">You&apos;ve used all free calls today. Resets at midnight IST.</p>
            <a className="ld-start ld-start-compact" href="/contact">
              Talk to our team <ArrowRight size={18} />
            </a>
          </>
        )}
      </div>

      <div className="ld-card-foot">
        <span>Telugu · Hindi · English</span>
        <span className="ld-sep">·</span>
        <span>code-mixed</span>
        <span className="ld-sep">·</span>
        <span>in your browser</span>
        {visitor && stage === 'idle' && (
          <button type="button" className="ld-notyou" onClick={() => setStage('form')}>
            Not {visitor.name}?
          </button>
        )}
      </div>
    </div>
  );
}

const STATE_TEXT = {
  connecting: 'Connecting…',
  initializing: 'RIA is joining…',
  listening: 'Listening…',
  thinking: 'Thinking…',
  speaking: 'RIA is speaking',
};

function LiveCall({ visitorName, onEnd }) {
  const { state, audioTrack, agentTranscriptions } = useVoiceAssistant();
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const room = useRoomContext();
  const hangUp = useHangUp(room, onEnd);
  const seconds = useCallGuards(state, hangUp);

  const caption = agentTranscriptions?.length ? agentTranscriptions[agentTranscriptions.length - 1].text : '';

  return (
    <div className="ld-live">
      <div className={`ld-orb ld-orb-live is-${state || 'connecting'}`}>
        <BarVisualizer className="ld-visualizer" state={state} barCount={5} track={audioTrack} />
      </div>
      <p className="ld-title">{STATE_TEXT[state] || (visitorName ? `Hi ${visitorName}` : 'Connected')}</p>
      <p className="ld-caption">{caption || 'Speak in Telugu, Hindi or English — RIA follows you.'}</p>
      <div className="ld-controls">
        <span className="ld-timer">{formatDuration(seconds)}</span>
        <button
          type="button"
          className={`ld-ctrl ${isMicrophoneEnabled ? '' : 'is-muted'}`}
          onClick={() => localParticipant?.setMicrophoneEnabled(!isMicrophoneEnabled)}
          aria-label={isMicrophoneEnabled ? 'Mute microphone' : 'Unmute microphone'}
        >
          {isMicrophoneEnabled ? <Mic size={18} /> : <MicOff size={18} />}
        </button>
        <button type="button" className="ld-end" onClick={() => hangUp()}>
          <PhoneOff size={18} /> End Call
        </button>
      </div>
    </div>
  );
}
