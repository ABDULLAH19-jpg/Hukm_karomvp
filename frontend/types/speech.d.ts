// Minimal typings for the Web Speech API, which TypeScript does not ship with.

interface SpeechRecognitionResultLike {
  readonly isFinal: boolean;
  readonly 0: { readonly transcript: string };
}

interface SpeechRecognitionResultListLike {
  readonly length: number;
  [index: number]: SpeechRecognitionResultLike;
}

interface SpeechRecognitionEventLike extends Event {
  readonly results: SpeechRecognitionResultListLike;
  readonly resultIndex: number;
}

interface SpeechRecognitionErrorEventLike extends Event {
  readonly error: string;
}

interface SpeechRecognitionLike extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  start(): void;
  abort(): void;
  stop(): void;
}

declare const webkitSpeechRecognition: { new (): SpeechRecognitionLike } | undefined;
declare const SpeechRecognition: { new (): SpeechRecognitionLike } | undefined;

interface Window {
  webkitSpeechRecognition?: { new (): SpeechRecognitionLike };
  SpeechRecognition?: { new (): SpeechRecognitionLike };
  webkitAudioContext?: typeof AudioContext;
  speechSynthesis: SpeechSynthesis;
}