'use client';

import { useState, useCallback, useEffect, useRef, Component } from 'react';
import { LiveKitRoom, RoomAudioRenderer } from '@livekit/components-react';
import '@livekit/components-styles';
import SimpleVoiceAssistant from './SimpleVoiceAssistant';
import { CloseIcon } from './Icons';
import {
  DAILY_LIMIT,
  LIVEKIT_URL,
  MIC_OPTIONS,
  callsLeftToday,
  fetchDemoToken,
  loadVisitor,
  roomErrorHandlers,
  saveVisitor,
  validateVisitor,
} from './demoSession';

class LiveKitErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error('LiveKit UI Error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            padding: '2rem',
            textAlign: 'center',
            background: '#ffffff',
          }}
        >
          <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>⚠️</div>
          <h3 style={{ color: '#0f172a', marginBottom: '0.5rem' }}>
            Something went wrong in the voice session
          </h3>
          <p
            style={{
              color: '#64748b',
              fontSize: '0.9rem',
              maxWidth: '400px',
              marginBottom: '1.5rem',
            }}
          >
            {this.state.error?.message ||
              'An unexpected error occurred while connecting audio.'}
          </p>
          <button
            onClick={() => {
              this.setState({ hasError: false, error: null });
              if (this.props.onReset) this.props.onReset();
            }}
            style={{
              padding: '0.75rem 1.5rem',
              background: '#4f46e5',
              color: '#ffffff',
              border: 'none',
              borderRadius: '8px',
              fontWeight: '600',
              cursor: 'pointer',
            }}
          >
            Try Again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const LiveKitModal = ({ setShowSupport }) => {
  const [isSubmittingName, setIsSubmittingName] = useState(true);
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [token, setToken] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [callsLeft, setCallsLeft] = useState(Infinity);
  const connectedAt = useRef(0);
  const endReason = useRef('');

  // Same visitor + daily limit as the homepage live demo card
  useEffect(() => {
    setCallsLeft(callsLeftToday());
    const saved = loadVisitor();
    if (saved) {
      setName(saved.name);
      setPhone(saved.phone);
    }
  }, []);

  const startCall = useCallback(async (visitor) => {
    if (callsLeftToday() === 0) {
      setCallsLeft(0);
      setErrorMsg("You've used all free calls today. Resets at midnight IST.");
      return;
    }
    try {
      setIsLoading(true);
      setErrorMsg('');
      connectedAt.current = 0;
      endReason.current = '';
      const { token: jwt } = await fetchDemoToken(visitor);
      setCallsLeft(callsLeftToday());
      setToken(jwt);
      setIsSubmittingName(false);
    } catch (error) {
      console.error('RIA token error:', error);
      setErrorMsg(error?.message || 'Could not connect to the RIA voice token server.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleNameSubmit = (event) => {
    event.preventDefault();
    const { visitor, error } = validateVisitor(name, phone);
    if (error) {
      setErrorMsg(error);
      return;
    }
    saveVisitor(visitor);
    startCall(visitor);
  };

  // Normal hang-up closes the modal; a failure (mic blocked, RIA didn't pick up,
  // time cap) returns to the form with the reason instead of vanishing
  const endCall = useCallback(
    (message = '') => {
      if (message) endReason.current = message;
      setToken(null);
      setIsSubmittingName(true);
      if (endReason.current) setErrorMsg(endReason.current);
      else setShowSupport(false);
    },
    [setShowSupport]
  );

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true">
      <div className="modal-content">
        <button
          type="button"
          className="modal-corner-close-btn"
          onClick={() => setShowSupport(false)}
          title="Close"
          aria-label="Close modal"
        >
          <CloseIcon size={20} />
        </button>

        <div className="support-room">
          {isSubmittingName ? (
            <div className="modal-form-container">
              <div className="modal-robot-badge">
                <div className="robot-badge-icon">🤖</div>
                <div className="robot-badge-pulse" />
              </div>

              <form onSubmit={handleNameSubmit} className="name-form" noValidate>
                <h2>Talk to RIA — AI Voice Demo</h2>
                <p className="form-subtext">
                  Experience RIA live in any language — Telugu, Hindi, Tamil, English and more. RIA greets you by name and
                  our team follows up on your number.
                  {DAILY_LIMIT > 0 && ` ${callsLeft} free ${callsLeft === 1 ? 'call' : 'calls'} left today.`}
                </p>

                {errorMsg && (
                  <div className="modal-error-banner">⚠️ {errorMsg}</div>
                )}

                <div className="input-group">
                  <input
                    type="text"
                    value={name}
                    onChange={(event) => {
                      setName(event.target.value);
                      setErrorMsg('');
                    }}
                    placeholder="Enter your name..."
                    required
                    autoFocus
                    autoComplete="name"
                    disabled={isLoading}
                    maxLength={60}
                  />
                </div>

                <div className="input-group modal-phone-group">
                  <span className="modal-phone-prefix">🇮🇳 +91</span>
                  <input
                    type="tel"
                    inputMode="numeric"
                    value={phone}
                    onChange={(event) => {
                      setPhone(event.target.value);
                      setErrorMsg('');
                    }}
                    placeholder="Mobile number"
                    required
                    autoComplete="tel-national"
                    disabled={isLoading}
                    maxLength={16}
                  />
                </div>

                <div className="form-buttons-group">
                  <button
                    type="submit"
                    className="start-call-btn"
                    disabled={isLoading || callsLeft === 0}
                  >
                    {isLoading ? (
                      <span className="btn-spinner-text">Connecting...</span>
                    ) : (
                      <span>🎙️ Start Live Call</span>
                    )}
                  </button>

                  <button
                    type="button"
                    className="cancel-button"
                    onClick={() => setShowSupport(false)}
                    disabled={isLoading}
                  >
                    Cancel
                  </button>
                </div>

                <div className="modal-form-footer">
                  <span>
                    ⚡ Speaks Telugu, Hindi &amp; English — calls capped at 5 minutes
                  </span>
                </div>
              </form>
            </div>
          ) : token ? (
            <LiveKitErrorBoundary
              onReset={() => {
                setToken(null);
                setIsSubmittingName(true);
              }}
            >
              <LiveKitRoom
                className="livekit-room-wrapper"
                serverUrl={LIVEKIT_URL}
                token={token}
                connect={true}
                video={false}
                audio={MIC_OPTIONS}
                options={{ audioCaptureDefaults: MIC_OPTIONS }}
                onDisconnected={() => endCall()}
                {...roomErrorHandlers(connectedAt, endCall)}
              >
                <RoomAudioRenderer />
                <SimpleVoiceAssistant visitorName={name} onDisconnect={endCall} />
              </LiveKitRoom>
            </LiveKitErrorBoundary>
          ) : null}
        </div>
      </div>
    </div>
  );
};

export default LiveKitModal;
