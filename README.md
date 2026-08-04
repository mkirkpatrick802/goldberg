# Goldberg

Goldberg is a Discord bot built for game development teams. He connects to your
SVN repository and Taiga project, tracks commits and sprint tasks, runs office
hours, takes meeting notes, and generally keeps the team moving — all with a bit
of attitude.

## Features

- **SVN commit notifications** — posts every new commit to a designated channel.
- **Sprint task reminders** — DMs each dev the tasks they still owe, every
  Tue/Fri/Sun, with a "Day X of Y · deadline" progress banner.
- **Office hours** — announces office hours when they start; `/officehours` and
  `/schedule` show who's on.
- **Meeting notes** — records a voice call and posts a transcript + summary.
- **Onboarding help** — `/repo`, `/builds`, `/documentation`.
- **Personality** — unprompted text barks in "bully" channels and when he's
  named. `/shutup` mutes them during serious conversations; `/wakeup` brings him
  back. (He no longer adds emoji *reactions* — those created false reply pings.)

## Documentation

- **[Overview](documentation/Overview.md)** — what Goldberg offers.
- **[Commands](documentation/Commands.md)** — full slash‑command reference.
- **[Automatic Behaviors](documentation/Automatic-Behaviors.md)** — everything
  he does on his own.
- **[Setup](documentation/SETUP.md)** — how to configure and self‑host him.

## Requirements

- Python 3.12+
- A Discord bot token
- An SVN repository and a Taiga project

## Quick Start

See the [Setup Guide](documentation/SETUP.md) for full instructions.
