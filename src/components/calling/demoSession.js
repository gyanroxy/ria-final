'use client';

// Shared call logic for every RIA voice entry point (homepage LiveDemoCard and
// the /demo LiveKitModal) so they behave identically.

import { useCallback, useEffect, useState } from 'react';

export const LIVEKIT_URL =
  process.env.NEXT_PUBLIC_LIVEKIT_URL || 'wss://collection-agent-r45cqdci.livekit.cloud';
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || '';

// Free demo calls per browser per day; 0 turns the limit off
export const DAILY_LIMIT = 0;
export const MAX_CALL_SECONDS = 5 * 60;
export const AGENT_JOIN_TIMEOUT = 20;

// Browser-side mic processing; the agent adds server-side noise cancellation on top
export const MIC_OPTIONS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };

const USAGE_KEY = 'ria-demo-usage';
const VISITOR_KEY = 'ria-demo-visitor';

// Day key in IST so the free-call counter resets at midnight IST
const istDay = () => new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);

function readStore(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
}

function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage blocked (private mode etc.) — demo still works, just isn't remembered
  }
}

export function callsUsedToday() {
  const usage = readStore(USAGE_KEY);
  return usage?.day === istDay() ? usage.count : 0;
}

// Calls left today, or Infinity when the daily limit is off
export function callsLeftToday() {
  return DAILY_LIMIT ? Math.max(0, DAILY_LIMIT - callsUsedToday()) : Infinity;
}

function recordCall() {
  const count = callsUsedToday() + 1;
  writeStore(USAGE_KEY, { day: istDay(), count });
  return count;
}

export function loadVisitor() {
  const saved = readStore(VISITOR_KEY);
  return saved?.name && saved?.phone ? saved : null;
}

export function saveVisitor(visitor) {
  writeStore(VISITOR_KEY, visitor);
}

const cleanPhone = (value) => {
  const digits = value.replace(/\D/g, '');
  return digits.length === 12 && digits.startsWith('91') ? digits.slice(2) : digits;
};

// Returns { visitor } or { error } for the name + mobile form
export function validateVisitor(name, phone) {
  const visitor = { name: name.trim().slice(0, 60), phone: cleanPhone(phone) };
  if (!visitor.name) return { error: 'Please enter your name.' };
  if (!/^[6-9]\d{9}$/.test(visitor.phone)) return { error: 'Please enter a valid 10-digit mobile number.' };
  return { visitor };
}

// Fetches a LiveKit token carrying the visitor's name + phone (the agent greets
// by name and saves the lead) and counts it against today's free calls.
export async function fetchDemoToken(visitor) {
  const query = `name=${encodeURIComponent(visitor.name)}&phone=${encodeURIComponent(visitor.phone)}`;
  let res = await fetch(`/api/getToken?${query}`, { cache: 'no-store' });
  if (!res.ok && res.status !== 400 && BACKEND_URL) {
    res = await fetch(`${BACKEND_URL.replace(/\/$/, '')}/getToken?${query}`, { cache: 'no-store' });
  }
  if (!res.ok) {
    let detail = 'Could not start the call. Please try again.';
    try {
      detail = (await res.json()).error || detail;
    } catch {
      // non-JSON error body
    }
    throw new Error(detail);
  }
  const token = await res.text();
  if (!token) throw new Error('Could not start the call. Please try again.');
  return { token, used: recordCall() };
}

// Errors in the first few seconds mean the call never really started; later
// ones (AirPods/headphone switching) must not hang up a live call
export const inCallSetup = (connectedAt) => !connectedAt || Date.now() - connectedAt < 6000;

export const MIC_BLOCKED_MESSAGE =
  'Microphone access is blocked. Allow the mic in your browser settings and try again.';

// LiveKitRoom error props shared by every entry point
export function roomErrorHandlers(connectedAtRef, endCall) {
  return {
    onConnected: () => {
      connectedAtRef.current = Date.now();
    },
    onMediaDeviceFailure: (failure) => {
      if (inCallSetup(connectedAtRef.current)) endCall(MIC_BLOCKED_MESSAGE);
      else console.warn('RIA demo: microphone issue during call', failure);
    },
    onError: (error) => {
      if (inCallSetup(connectedAtRef.current)) {
        endCall(`The call dropped (${error?.message || 'connection error'}). Please try again.`);
      } else {
        console.warn('RIA demo: non-fatal call error', error);
      }
    },
  };
}

// Call timer + 5-minute cap + "agent never joined" timeout. Use inside a LiveKitRoom.
export function useCallGuards(agentState, hangUp) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const agentJoined = agentState && !['disconnected', 'connecting', 'initializing'].includes(agentState);

  useEffect(() => {
    if (seconds >= MAX_CALL_SECONDS) hangUp('Demo calls are limited to 5 minutes. Talk again or contact our team.');
    else if (!agentJoined && seconds >= AGENT_JOIN_TIMEOUT) hangUp("RIA didn't pick up. Please try again in a minute.");
  }, [seconds, agentJoined, hangUp]);

  return seconds;
}

export const formatDuration = (seconds) =>
  `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;

// Reports the reason, then disconnects. The reason must land first: the room's
// own onDisconnected fires during disconnect() and would otherwise close the UI
// (e.g. the /demo modal) before the reason could be shown.
export function useHangUp(room, onEnd) {
  return useCallback(
    async (message) => {
      onEnd?.(message);
      try {
        if (room && room.state !== 'disconnected') await room.disconnect(true);
      } catch {
        // already disconnected
      }
    },
    [room, onEnd]
  );
}
