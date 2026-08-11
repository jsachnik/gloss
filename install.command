#!/bin/bash
# One-time setup for Gloss.  Double-click this file in Finder.
#
# The virtual environment is built at ~/.venvs/gloss -- deliberately OUTSIDE
# this project folder, because the Desktop is iCloud-synced and a venv is
# thousands of tiny files that iCloud should never try to sync.

set -e
cd "$(dirname "$0")"

VENV="$HOME/.venvs/gloss"
ENV_FILE=".env"

echo "=============================================="
echo "  Gloss -- setup"
echo "=============================================="
echo

# --- Python ----------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found.  Install Python 3.10+ and run this again."
    exit 1
fi
echo "Python:  $(python3 --version)"

# --- Virtual environment ---------------------------------------------------
if [ -d "$VENV" ]; then
    echo "Environment already exists at $VENV -- updating packages."
else
    echo "Creating environment at $VENV"
    mkdir -p "$HOME/.venvs"
    python3 -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt
echo "Dependencies installed."
echo

# --- Provider --------------------------------------------------------------
echo "----------------------------------------------"
echo "  Which AI provider should Gloss use?"
echo "----------------------------------------------"
echo "  1) OpenAI          (api.openai.com — needs a paid key)"
echo "  2) Anthropic       (Claude, via their OpenAI-compatible endpoint)"
echo "  3) TAMU AI         (free with a Texas A&M NetID)"
echo "  4) OpenRouter      (one key, most vendors)"
echo "  5) Ollama          (a model on this Mac — no key, no cost)"
echo "  6) Something else  (any OpenAI-compatible endpoint)"
if [ -f "$ENV_FILE" ]; then
    CURRENT=$(sed -n -E 's|^GLOSS_PROVIDER[[:space:]]*=[[:space:]]*(.*)$|\1|p' "$ENV_FILE" | head -1)
    echo "  7) Leave unchanged (currently \"${CURRENT:-unset}\")"
fi
echo

PROVIDER=""
while [ -z "$PROVIDER" ]; do
    printf "Enter a number: "
    read -r choice
    case "$choice" in
        1) PROVIDER="openai";     KEY_VAR="OPENAI_API_KEY";     SUGGEST="gpt-5.4-mini" ;;
        2) PROVIDER="anthropic";  KEY_VAR="ANTHROPIC_API_KEY";  SUGGEST="claude-sonnet-5" ;;
        3) PROVIDER="tamu";       KEY_VAR="TAMU_AI_API_KEY";    SUGGEST="protected.Claude Sonnet 4.6" ;;
        4) PROVIDER="openrouter"; KEY_VAR="OPENROUTER_API_KEY"; SUGGEST="" ;;
        5) PROVIDER="ollama";     KEY_VAR="";                   SUGGEST="" ;;
        6) PROVIDER="custom";     KEY_VAR="GLOSS_API_KEY";      SUGGEST="" ;;
        7) if [ -f "$ENV_FILE" ]; then PROVIDER="keep"; else echo "Please enter 1-6."; fi ;;
        *) echo "Please enter a number from the list." ;;
    esac
done

if [ "$PROVIDER" = "keep" ]; then
    echo "Leaving .env as it is."
else
    BASE_URL=""
    if [ "$PROVIDER" = "custom" ]; then
        printf "Base URL of the endpoint (e.g. http://localhost:1234/v1): "
        read -r BASE_URL
    fi

    # Ollama: offer the models actually installed, so nobody types a tag by hand.
    MODEL=""
    if [ "$PROVIDER" = "ollama" ]; then
        echo
        echo "Looking for models on this Mac..."
        MODELS=$("$VENV/bin/python" -c "import llm; print('\n'.join(llm.local_models()))" 2>/dev/null || true)
        if [ -z "$MODELS" ]; then
            echo "  No local server answered at http://localhost:11434."
            echo "  Start Ollama first — open a Terminal and run:  ollama serve"
            echo "  Then add a model with:  ollama pull qwen3.6"
            echo "  Re-run this installer once it's running."
            exit 1
        fi
        echo "Models available:"
        i=0; while IFS= read -r m; do i=$((i+1)); echo "  $i) $m"; done <<< "$MODELS"
        while [ -z "$MODEL" ]; do
            printf "Enter a number: "
            read -r n
            MODEL=$(echo "$MODELS" | sed -n "${n}p" 2>/dev/null || true)
            [ -z "$MODEL" ] && echo "Please enter a number from the list."
        done
    else
        echo
        if [ -n "$SUGGEST" ]; then
            printf "Model to use [%s]: " "$SUGGEST"
            read -r MODEL
            [ -z "$MODEL" ] && MODEL="$SUGGEST"
        else
            # No default worth assuming for this provider.
            while [ -z "$MODEL" ]; do
                printf "Model id to use (required for this provider): "
                read -r MODEL
            done
        fi
    fi

    # Key. Read silently so it never appears on screen or in scrollback.
    KEY=""
    if [ -n "$KEY_VAR" ]; then
        EXISTING=$(printenv "$KEY_VAR" || true)
        if [ -n "$EXISTING" ]; then
            echo
            echo "Found $KEY_VAR already set in your shell — using that."
        elif [ "$PROVIDER" = "tamu" ] && [ -f "$HOME/.config/tamu-ai/api-key" ]; then
            echo
            echo "Found your key at ~/.config/tamu-ai/api-key — using that."
        else
            echo
            case "$PROVIDER" in
                openai)     echo "Get a key from https://platform.openai.com/api-keys" ;;
                anthropic)  echo "Get a key from https://console.anthropic.com/settings/keys" ;;
                tamu)       echo "Get a key from chat.tamu.ai -> Settings -> API Key" ;;
                openrouter) echo "Get a key from https://openrouter.ai/keys" ;;
            esac
            printf "Paste your API key (it won't be shown): "
            read -rs KEY
            echo
        fi
    fi

    # Write .env.  Only what's needed; nothing echoed back.
    {
        echo "# Written by install.command. See .env.example for every option."
        echo "GLOSS_PROVIDER=$PROVIDER"
        echo "GLOSS_MODEL=$MODEL"
        [ -n "$BASE_URL" ] && echo "GLOSS_BASE_URL=$BASE_URL"
        [ -n "$KEY" ] && echo "$KEY_VAR=$KEY"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Wrote .env (readable only by you)."
fi

# --- Verify ----------------------------------------------------------------
echo
echo "Checking the configuration..."
if "$VENV/bin/python" llm.py; then
    echo
    echo "=============================================="
    echo "  Setup complete.  Double-click run.command"
    echo "  to start reading."
    echo "=============================================="
else
    echo
    echo "  Setup finished, but that check didn't pass — see the message above."
    echo "  Gloss will still open and display PDFs; summaries need a working"
    echo "  provider. Re-run this installer to change it."
fi
echo
