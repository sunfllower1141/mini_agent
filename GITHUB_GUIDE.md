# GitHub Fork & Push Guide — for beginners

Follow these steps to get your fork of mini_agent on GitHub so your friend can test it.

---

## Step 1: Create a GitHub Account

Go to https://github.com and click **Sign up**.

- Pick a username (e.g., your name or nickname)
- Use any email — only needed for verification
- Choose the **Free** plan (no credit card required)
- Verify your email (GitHub sends a confirmation link)

> Done in 2 minutes. No coding required.

---

## Step 2: Fork the Original Repo

A "fork" is your own copy of someone else's project on GitHub.

1. Open your browser to: **https://github.com/GabrielMalone/mini_agent**
2. Click the **Fork** button (top-right corner, near the star button)
3. Leave all settings as default, click **Create fork**
4. After a few seconds you'll be on your fork: `https://github.com/YOUR_USERNAME/mini_agent`

> This is your copy. Changes you make here won't affect the original.

---

## Step 3: Point Your Local Repo to Your Fork

Your local folder is currently connected to GabrielMalone's repo. We need to point it to your fork instead.

Open **Command Prompt** (Win+R, `cmd`, Enter) and run:

```bat
cd E:\mini_agent
git remote set-url origin https://github.com/YOUR_USERNAME/mini_agent.git
```

Replace `YOUR_USERNAME` with your actual GitHub username.

Verify it worked:
```bat
git remote -v
```

Should show:
```
origin  https://github.com/YOUR_USERNAME/mini_agent.git (fetch)
origin  https://github.com/YOUR_USERNAME/mini_agent.git (push)
```

---

## Step 4: Commit Your Changes

Now we'll save (commit) all your local changes and push them to your fork.

```bat
cd E:\mini_agent

:: Add all changed and new files
git add -A

:: Create a commit with a message
git commit -m "Windows fixes: run_shell timeout, Defender workaround, install guide"
```

> If git asks you to set your name/email, run these two commands first:
> ```bat
> git config --global user.name "Your Name"
> git config --global user.email "your@email.com"
> ```
> Then re-run `git commit -m "..."`.

---

## Step 5: Push to Your Fork

```bat
git push origin master
```

> If this is your first time pushing to GitHub, a browser window may open asking you to **Sign in with GitHub**. Click **Authorize** or enter your GitHub username + password.

Alternative: use a **Personal Access Token** instead of a password:
1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Check the `repo` scope
4. Generate and copy the token
5. When git asks for a password, paste the token (it won't show on screen)

> GitHub no longer accepts passwords for git operations — only tokens.

---

## Step 6: Verify

Open `https://github.com/YOUR_USERNAME/mini_agent` in your browser. You should see:
- Your `WINDOWS_INSTALL.md` file
- The updated `requirements.txt`
- The commit message you just pushed

---

## Step 7: Share with Your Friend

Send your friend this link:

```
https://github.com/YOUR_USERNAME/mini_agent
```

They can clone it with:
```bat
git clone https://github.com/YOUR_USERNAME/mini_agent.git
cd mini_agent
setup.bat
```

And they can read your Windows guide at `WINDOWS_INSTALL.md`.

---

## Full Commands (Copy-Paste)

Fill in your username, then run these in order:

```bat
:: Step 3: Point to your fork
cd E:\mini_agent
git remote set-url origin https://github.com/YOUR_USERNAME/mini_agent.git

:: Step 4: Commit everything
git add -A
git commit -m "Windows fixes: run_shell timeout, Defender workaround, install guide"

:: Step 5: Push
git push origin master
```

That's it!

---

## Troubleshooting

### "fatal: unable to access ... SSL certificate problem"
Temporary workaround (not secure for long-term):
```bat
git config --global http.sslVerify false
git push origin master
git config --global http.sslVerify true
```

### "Permission denied (publickey)"
You're trying to use SSH without a key. Switch to HTTPS:
```bat
git remote set-url origin https://github.com/YOUR_USERNAME/mini_agent.git
```

### "failed to push some refs"
Your fork may have commits you don't have locally. Force push (safe since it's your fork):
```bat
git push origin master --force
```

### Token authentication not working
- GitHub now requires **fine-grained personal access tokens**
- Go to: https://github.com/settings/tokens?type=beta
- Click "Generate new token"
- Set Repository access to "Only select repositories" → choose mini_agent
- Under Permissions → Contents → set to "Read and write"
- Copy the token immediately (it won't be shown again)
- Use it as your password when pushing

### "Updates were rejected because the remote contains work that you do not have locally"
This happens if you accidentally committed to the fork's web interface. Run:
```bat
git pull origin master --rebase
git push origin master
```

### "git is not recognized as an internal or external command"
Git is not installed or not in PATH:
```bat
winget install Git.Git
```
Close and reopen Command Prompt, then retry.

---

## Next Time You Make Changes

After making more fixes, just:
```bat
cd E:\mini_agent
git add -A
git commit -m "description of what you changed"
git push origin master
```

Your friend can pull your latest changes with:
```bat
cd mini_agent
git pull
```
