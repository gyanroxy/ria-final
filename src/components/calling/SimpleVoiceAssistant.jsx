'use client';

import { useState, useEffect, useRef } from 'react';
import {
  useVoiceAssistant,
  BarVisualizer,
  useTrackTranscription,
  useLocalParticipant,
  useRoomContext,
} from '@livekit/components-react';
import { Track } from 'livekit-client';
import RobotAvatar from './RobotAvatar';
import {
  MicIcon,
  MicOffIcon,
  ChatIcon,
  SpeakerOnIcon,
  SpeakerOffIcon,
  PhoneEndIcon,
  CloseIcon,
  CopyIcon,
  CheckIcon,
} from './Icons';
import { formatDuration, useCallGuards, useHangUp } from './demoSession';
import './SimpleVoiceAssistant.css';

const STATE_TEXT = {
  connecting: 'Connecting to RIA…',
  initializing: 'RIA is joining…',
  listening: 'Listening to you',
  thinking: 'Thinking…',
  speaking: 'RIA is speaking',
};

const SimpleVoiceAssistant = ({ visitorName, onDisconnect }) => {
  const { state, audioTrack, agentTranscriptions } = useVoiceAssistant();
  const localParticipantState = useLocalParticipant();
  const room = useRoomContext();
  // Same timer, 5-minute cap and "RIA didn't pick up" timeout as the homepage card
  const hangUp = useHangUp(room, onDisconnect);
  const seconds = useCallGuards(state, hangUp);

  const isMicrophoneEnabled = localParticipantState?.isMicrophoneEnabled ?? false;
  const localParticipant = localParticipantState?.localParticipant;
  const microphoneTrack = localParticipantState?.microphoneTrack;

  // Safe track reference for user transcription to prevent getTrackReferenceId crash
  const userTrackRef =
    microphoneTrack && localParticipant
      ? {
          publication: microphoneTrack,
          source: Track.Source.Microphone,
          participant: localParticipant,
        }
      : undefined;

  const { segments: userTranscriptions } = useTrackTranscription(userTrackRef);

  const [messages, setMessages] = useState([]);
  const [showTranscript, setShowTranscript] = useState(false);
  const [isSpeakerMuted, setIsSpeakerMuted] = useState(false);
  const [copied, setCopied] = useState(false);
  const chatEndRef = useRef(null);

  // Sync and sort conversation transcript
  useEffect(() => {
    const allMessages = [
      ...(agentTranscriptions?.map((t) => ({ ...t, type: 'agent' })) ?? []),
      ...(userTranscriptions?.map((t) => ({ ...t, type: 'user' })) ?? []),
    ].sort((a, b) => (a.firstReceivedTime ?? 0) - (b.firstReceivedTime ?? 0));
    setMessages(allMessages);
  }, [agentTranscriptions, userTranscriptions]);

  // Auto-scroll transcript when new messages arrive
  useEffect(() => {
    if (showTranscript && chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, showTranscript]);

  // Microphone toggle handler
  const handleToggleMic = async () => {
    try {
      if (localParticipant) {
        await localParticipant.setMicrophoneEnabled(!isMicrophoneEnabled);
      }
    } catch (err) {
      console.error('Failed to toggle microphone:', err);
    }
  };

  // Speaker / Audio Output toggle handler
  const handleToggleSpeaker = () => {
    try {
      const nextMute = !isSpeakerMuted;
      setIsSpeakerMuted(nextMute);
      if (audioTrack?.publication?.track?.mediaStreamTrack) {
        audioTrack.publication.track.mediaStreamTrack.enabled = !nextMute;
      }
    } catch (err) {
      console.error('Failed to toggle speaker audio:', err);
    }
  };

  // End Call: plain hang-up (no reason) closes the modal
  const handleDisconnect = () => hangUp();

  // Copy transcript to clipboard
  const handleCopyTranscript = () => {
    if (messages.length === 0) return;
    const textToCopy = messages
      .map((m) => `${m.type === 'agent' ? 'RIA' : 'You'}: ${m.text}`)
      .join('\n');
    navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Format timestamp nicely
  const formatTime = (timestamp) => {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <div className={`modern-assistant-root ${showTranscript ? 'transcript-open' : ''}`}>
      {/* Top Header Bar */}
      <div className="assistant-header">
        <div className="assistant-brand">
          <div className="brand-dot" />
          <span className="brand-title">RIA Voice Assistant</span>
          <span className="live-pill">LIVE CALL</span>
        </div>
        <div className="header-meta">
          <span className="room-indicator">
            {STATE_TEXT[state] || (visitorName ? `On call with ${visitorName}` : 'Secure LiveKit Call')}
            {' · '}
            {formatDuration(seconds)}
          </span>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="assistant-main-layout">
        {/* Central Stage with Robot Avatar */}
        <div className="avatar-stage-area">
          {/* Interactive Talking White Robot */}
          <RobotAvatar state={state || 'idle'} />

          {/* Minimalist Live Audio Visualizer Bar */}
          <div className="audio-visualizer-dock">
            {audioTrack ? (
              <BarVisualizer
                state={state}
                barCount={15}
                trackRef={audioTrack}
              />
            ) : (
              <div className="visualizer-connecting">
                <span className="visualizer-connecting-dot" />
                <span className="visualizer-connecting-text">
                  {state === 'connecting' ? 'Connecting audio...' : 'Audio Ready'}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Conversation Transcript Drawer / Sheet */}
        <div className={`transcript-drawer ${showTranscript ? 'visible' : ''}`}>
          <div className="drawer-header">
            <div className="drawer-title-group">
              <ChatIcon size={18} />
              <h3>Live Conversation</h3>
              <span className="message-count-badge">{messages.length}</span>
            </div>
            <div className="drawer-actions">
              {messages.length > 0 && (
                <button
                  type="button"
                  className="drawer-tool-btn"
                  onClick={handleCopyTranscript}
                  title="Copy Transcript"
                >
                  {copied ? <CheckIcon size={16} /> : <CopyIcon size={16} />}
                  <span>{copied ? 'Copied' : 'Copy'}</span>
                </button>
              )}
              <button
                type="button"
                className="drawer-close-btn"
                onClick={() => setShowTranscript(false)}
                title="Close Transcript"
              >
                <CloseIcon size={18} />
              </button>
            </div>
          </div>

          <div className="drawer-body">
            {messages.length === 0 ? (
              <div className="empty-transcript">
                <div className="empty-icon">💬</div>
                <p className="empty-headline">No messages yet</p>
                <p className="empty-sub">Speak in Telugu, Hindi or English — RIA follows you.</p>
              </div>
            ) : (
              <div className="chat-messages-list">
                {messages.map((msg, idx) => (
                  <div key={msg.id || idx} className={`chat-bubble-row ${msg.type}`}>
                    <div className="bubble-sender-avatar">
                      {msg.type === 'agent' ? '🤖' : '👤'}
                    </div>
                    <div className="bubble-content-box">
                      <div className="bubble-meta">
                        <span className="bubble-author">
                          {msg.type === 'agent' ? 'RIA' : 'You'}
                        </span>
                        {msg.firstReceivedTime && (
                          <span className="bubble-time">
                            {formatTime(msg.firstReceivedTime)}
                          </span>
                        )}
                      </div>
                      <p className="bubble-text">{msg.text}</p>
                    </div>
                  </div>
                ))}
                <div ref={chatEndRef} />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Floating Modern Pill Dock Controls */}
      <div className="control-dock-container">
        <div className="control-dock-pill">
          {/* 1. Microphone Toggle Button */}
          <button
            type="button"
            className={`dock-btn mic-btn ${isMicrophoneEnabled ? 'active' : 'muted'}`}
            onClick={handleToggleMic}
            title={isMicrophoneEnabled ? 'Mute Microphone' : 'Unmute Microphone'}
            aria-label={isMicrophoneEnabled ? 'Mute Microphone' : 'Unmute Microphone'}
          >
            {isMicrophoneEnabled ? <MicIcon size={22} /> : <MicOffIcon size={22} />}
            <span className="dock-tooltip">
              {isMicrophoneEnabled ? 'Mic On' : 'Mic Off'}
            </span>
          </button>

          {/* 2. Conversation / Transcript Toggle Button */}
          <button
            type="button"
            className={`dock-btn chat-btn ${showTranscript ? 'active' : ''}`}
            onClick={() => setShowTranscript((prev) => !prev)}
            title={showTranscript ? 'Hide Conversation' : 'Show Conversation'}
            aria-label={showTranscript ? 'Hide Conversation' : 'Show Conversation'}
          >
            <ChatIcon size={22} />
            {messages.length > 0 && (
              <span className="chat-unread-badge">{messages.length}</span>
            )}
            <span className="dock-tooltip">
              {showTranscript ? 'Hide Chat' : 'View Chat'}
            </span>
          </button>

          {/* 3. Speaker Output Mute Button */}
          <button
            type="button"
            className={`dock-btn speaker-btn ${isSpeakerMuted ? 'muted' : 'active'}`}
            onClick={handleToggleSpeaker}
            title={isSpeakerMuted ? 'Unmute Speaker' : 'Mute Speaker'}
            aria-label={isSpeakerMuted ? 'Unmute Speaker' : 'Mute Speaker'}
          >
            {isSpeakerMuted ? <SpeakerOffIcon size={22} /> : <SpeakerOnIcon size={22} />}
            <span className="dock-tooltip">
              {isSpeakerMuted ? 'Audio Muted' : 'Audio On'}
            </span>
          </button>

          {/* Dock Divider */}
          <div className="dock-divider" />

          {/* 4. End Call / Disconnect Button */}
          <button
            type="button"
            className="dock-btn end-call-btn"
            onClick={handleDisconnect}
            title="End Voice Call"
            aria-label="End Voice Call"
          >
            <PhoneEndIcon size={22} />
            <span className="dock-tooltip">End Call</span>
          </button>
        </div>
      </div>
    </div>
  );
};

export default SimpleVoiceAssistant;
