# Reference voices

Put reference clips for the Thai TTS sidecar here, then point
`../voices.yaml` at them.

## Recording a good reference clip

| Requirement | Why |
|---|---|
| 5–15 seconds | Shorter loses timbre; longer adds nothing and slows every request |
| One speaker, no background | F5-TTS clones whatever it hears, including the noise |
| No music, reverb, or compression artefacts | These become permanent features of the voice |
| Natural pace and pitch | The clip sets the baseline; `speed:` only nudges it |
| WAV, mono, 24 kHz | Native rate — anything else is resampled on load |

## Transcript accuracy matters

`ref_text` must match the audio exactly, including punctuation. A mismatched
transcript is the single most common cause of garbled or unstable output —
the model uses the pair to learn the speaker's phoneme realisation, so a wrong
transcript teaches it the wrong mapping.

## Converting a clip

```bash
ffmpeg -i input.m4a -ac 1 -ar 24000 -sample_fmt s16 jarvis_ref.wav
```

## Licensing

Only use voices you have the right to clone. Cloning a real person's voice
without their consent is unlawful in many jurisdictions and is not a supported
use of this project.

Clips are gitignored by default — see `.gitignore`.
