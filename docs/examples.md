# Examples Guide

Practical examples for using the Local Coding Agent effectively.

## Table of Contents

1. [Code Generation](#code-generation)
2. [Code Review](#code-review)
3. [Git Workflows](#git-workflows)
4. [File Operations](#file-operations)
5. [Multi-turn Conversations](#multi-turn-conversations)
6. [Best Practices](#best-practices)

---

## Code Generation

### Simple Script

```
> Write a Python script that reads a CSV file and prints the sum of a column
```

### Function with Tests

```
> Write a Python function to validate email addresses, and include unit tests
```

### Full Module

```
> Create a Python module for interacting with a REST API. Include:
- A class for the API client
- Methods for GET, POST, PUT, DELETE requests
- Error handling
- Type hints
```

### Refactoring

```
> Refactor this function to be more readable and add docstrings:
[paste function code]
```

---

## Code Review

### General Review

```
> Review the changes in my current git diff
```

### Security Focus

```
> Check this code for security vulnerabilities, especially SQL injection and XSS
```

### Performance Review

```
> Analyze this code for performance issues and suggest optimizations
```

### Style Guide Compliance

```
> Review this code against PEP 8 style guide and suggest improvements
```

---

## Git Workflows

### Daily Standup

```
> Show me my uncommitted changes
> What files did I modify today?
> Show me the git log for the last 5 commits
```

### Before Committing

```
> Review my staged changes
> Are there any obvious bugs in what I'm about to commit?
> Generate a commit message for my changes
```

### Branch Management

```
> What branches exist in this repo?
> Compare my feature branch with main
> Show me commits on the feature/new-auth branch
```

---

## File Operations

### Project Setup

```
> Create a new Python project structure with:
- src/ directory
- tests/ directory
- pyproject.toml
- README.md
- .gitignore
```

### Code Exploration

```
> Find all Python files in the project
> Show me the file structure
> Find all files containing "database"
```

### File Modification

```
> Add error handling to main.py
> Rename all instances of "user_id" to "userId" in this file
> Add type hints to this function

### Surgical Edits (Precise Patching)

```
> Fix the typo in src/auth.py by replacing 'authentcate' with 'authenticate'
```

The agent will use an `EDIT:` block to target only that line:
```
EDIT: src/auth.py
<<<OLD
def authentcate(user):
===
def authenticate(user):
>>>
```
```

---

## Multi-turn Conversations

### Session 1: Initial Request

```
> Create a Flask web application with user authentication
```

### Session 2: Iteration

```
> Add password reset functionality
> Add unit tests for the authentication flow
```

### Session 3: Refinement

```
> Add rate limiting to the login endpoint
> Deploy this to Docker
```

### Resuming Later

```bash
# List sessions to find the right one
python -m local_coding_agent --list-sessions

# Resume the Flask project session
python -m local_coding_agent --session session_flask_project

# Continue working
> Add a user profile page
```

---

## Effective Prompting

### Be Specific

```
# Less effective
> Fix the bug

# More effective
> Fix the bug in the user authentication function where login fails 
# when the password contains special characters like @ or #
```

### Provide Context

```
# Less effective
> Write tests

# More effective
> Write unit tests for the calculate_total function, following the 
# existing test patterns in tests/test_calculations.py
```

### Break Down Complex Tasks

```
# Instead of:
> Build a complete e-commerce platform

# Try:
> Create the product data model
> Add the shopping cart functionality
> Implement checkout flow
> Add payment integration (stub)
```

### Specify Format

```
> Export the data to CSV with columns: id, name, email, created_at
```

---

## Best Practices

### 1. Start Small

Begin with simple tasks to establish context before complex requests:

```
# First, simple task
> Add two numbers in Python

# Then, context-aware request
> Now create a calculator class based on what you just wrote
```

### 2. Use Sessions for Projects

Maintain context across related tasks:

```bash
# Start a session for your project
python -m local_coding_agent --session my-web-app

# All subsequent tasks share context
> Create a Flask app
> Add user authentication
> Write tests
> Deploy to Docker
```

### 3. Review Before Committing

Always review AI-generated code:

```
> Show me the changes you made
> Are there any potential bugs?
> What edge cases might fail?
```

### 4. Use Version Control

Commit AI-generated code like any other code:

```
> Generate a commit message for my changes
> What should I include in my commit message?
```

### 5. Test AI Outputs

Always verify AI-generated code works:

```
> Run the tests
> Execute the script and verify output
> Check for syntax errors
```

---

## Example Workflows

### Bug Fix Workflow

1. **Identify the bug**
   ```
   > There's a bug in user registration when email contains "+"
   ```

2. **Get context**
   ```
   > Show me the registration code
   ```

3. **Request fix**
   ```
   > Fix the email validation to handle "+" characters
   ```

4. **Test the fix**
   ```
   > Write tests for this edge case
   > Run the tests
   ```

5. **Commit**
   ```
   > Show me the changes
   > Generate a commit message
   ```

### Feature Development Workflow

1. **Plan the feature**
   ```
   > Help me design a user notification system
   ```

2. **Create the structure**
   ```
   > Create the notification module with:
   - EmailNotification
   - SMSNotification
   - PushNotification classes
   ```

3. **Add tests**
   ```
   > Write unit tests for each notification type
   ```

4. **Review and refine**
   ```
   > What improvements would you suggest?
   ```

5. **Commit**
   ```
   > Generate a commit message
   ```

### Code Review Workflow

1. **Show changes**
   ```
   > Show me my unstaged changes
   > Show me my staged changes
   ```

2. **Request review**
   ```
   > Review this code for:
   - Security issues
   - Performance problems
   - Code style violations
   ```

3. **Address feedback**
   ```
   > Fix the security issues you identified
   ```

---

## Troubleshooting

### Code Doesn't Work

```
> This code throws an error: [paste error]
> Fix the bug
```

### Unexpected Output

```
> The script outputs 5 instead of 10
> Debug this function
```

### Performance Issues

```
> This query is slow with large datasets
> Optimize for performance
```

### Don't Understand Code

```
> Explain what this function does
> Add detailed comments to this code
```
