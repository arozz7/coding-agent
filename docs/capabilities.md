# Agent Capabilities

## What the Agent Can Do

### Code Generation

- **Write scripts** in any programming language (Python, JavaScript, TypeScript, Go, Rust, Java, C#, etc.)
- **Create modules and packages** with proper structure
- **Generate boilerplate** for common patterns (APIs, classes, functions)
- **Implement algorithms** from descriptions
- **Write SQL queries** and database operations
- **Create configuration files** (JSON, YAML, TOML, etc.)

### Code Analysis

- **Read and understand** existing codebases
- **Find bugs** and suggest fixes
- **Identify performance issues**
- **Detect security vulnerabilities**
- **Explain code** in human-readable terms
- **Suggest improvements** and best practices

### Code Modification

- **Add features** to existing code
- **Refactor** for readability and maintainability
- **Add error handling** and validation
- **Add type hints** and documentation
- **Migrate code** between languages or frameworks
- **Update deprecated patterns**

### Testing

- **Write unit tests** following test patterns
- **Generate test data** and fixtures
- **Add integration tests**
- **Cover edge cases**
- **Run tests** and interpret results

### Git Operations

- **View repository state** (status, diff, log)
- **Stage and commit** changes
- **Manage branches** (list, create, switch)
- **Review changes** before committing
- **Generate commit messages**
- **View history** and blame

### File Operations

- **Create, read, update, delete** files
- **Search for files** by pattern
- **Explore directory structures**
- **Batch operations** on multiple files
- **Path traversal protection** (security)

### Conversational AI

- **Maintain context** across multiple turns
- **Remember previous discussions** within a session
- **Build upon earlier work** incrementally
- **Answer technical questions**
- **Explain concepts** and patterns

---

## What the Agent Cannot Do

### Security Limitations

- **No network access** in sandboxed mode (by design)
- **No sudo/admin actions** without explicit permission
- **Path traversal protection** prevents accessing files outside workspace
- **Command validation** blocks dangerous operations

### Technical Limitations

- **No execution** of arbitrary code outside sandbox
- **No database connections** unless configured
- **No external API calls** (except to configured LLM endpoints)
- **Context window limits** apply to very long conversations
- **Model capabilities** depend on configured LLM

### Physical Limitations

- **Cannot interact** with GUI applications
- **Cannot browse the web**
- **Cannot send emails** or notifications
- **Cannot directly modify** system files
- **Cannot install software** (only write files)

---

## Capabilities by Category

### Code Generation

| Capability | Supported | Notes |
|-----------|-----------|-------|
| Python | ✅ Full | Type hints, async, classes |
| JavaScript/TypeScript | ✅ Full | ES6+, TypeScript types |
| Go | ✅ Full | Goroutines, interfaces |
| Rust | ✅ Full | Traits, lifetimes |
| Java | ✅ Full | OOP, generics |
| C# | ✅ Full | LINQ, async/await |
| SQL | ✅ Full | Queries, DDL, procedures |
| Shell/Bash | ✅ Full | Scripts, pipelines |
| HTML/CSS | ✅ Full | Responsive, modern CSS |
| JSON/YAML/TOML | ✅ Full | Config files |

### Code Analysis

| Capability | Supported | Notes |
|-----------|-----------|-------|
| Syntax errors | ✅ | Immediate detection |
| Type errors | ✅ | With type hints |
| Security issues | ✅ | Common vulnerabilities |
| Performance issues | ✅ | Algorithm complexity |
| Code smells | ✅ | Style violations |
| Logic errors | ⚠️ | Requires context |
| Memory leaks | ⚠️ | Requires full context |

### Git Operations

| Capability | Supported | Notes |
|-----------|-----------|-------|
| `git status` | ✅ | Short and verbose |
| `git diff` | ✅ | Staged and unstaged |
| `git commit` | ✅ | With message |
| `git log` | ✅ | Recent history |
| `git branch` | ✅ | List all |
| `git add` | ✅ | Stage files |
| `git restore` | ✅ | Unstage files |
| `git merge` | ⚠️ | Complex cases |
| `git rebase` | ⚠️ | Not recommended |
| `git push/pull` | ❌ | Network disabled |

### File Operations

| Capability | Supported | Notes |
|-----------|-----------|-------|
| Read files | ✅ | With encoding handling |
| Write files | ✅ | Create/update |
| Delete files | ✅ | With confirmation |
| Glob/search | ✅ | Pattern matching |
| Directory listing | ✅ | Recursive option |
| Symbolic links | ⚠️ | Restricted |
| Permissions | ⚠️ | Read-only info |
| Hard links | ❌ | Not supported |

---

## Model-Dependent Capabilities

Some capabilities depend on the configured LLM:

### Reasoning Quality

| Model Size | Best For |
|-----------|----------|
| 7B params | Quick tasks, simple code |
| 30B params | Most coding tasks |
| 70B+ params | Complex reasoning, architecture |

### Context Window

| Context Size | Max Conversation |
|-------------|-----------------|
| 8K tokens | ~10-15 messages |
| 32K tokens | ~40-50 messages |
| 128K tokens | ~150+ messages |
| 256K tokens | Full codebase context |

### Coding Optimization

Some models are specifically trained for code:

- `qwen2.5-coder` - Code-optimized
- `starcoder` - Code-optimized  
- `codellama` - Code-optimized
- `glm-4` - General purpose

---

## Best Use Cases

### Ideal Tasks

1. **Boilerplate generation** - Set up project structure
2. **Code conversion** - Translate between languages
3. **Test writing** - Generate unit tests
4. **Documentation** - Add docstrings, comments
5. **Bug explanation** - Understand and explain errors
6. **Refactoring** - Improve existing code
7. **Algorithm implementation** - Write from pseudocode
8. **SQL queries** - Generate from descriptions

### Challenging Tasks

1. **Debugging complex issues** - May require extensive context
2. **Large refactors** - Best done incrementally
3. **Multi-file architecture** - Requires careful prompting
4. **Security-critical code** - Review all AI-generated code
5. **Performance optimization** - May need profiling data

### Tasks to Avoid

1. **Generating passwords/keys** - Use proper tools
2. **SQL injection exploits** - Security risk
3. **Phishing content** - Never generate
4. **Malware** - Never generate
5. **Personally identifiable info** - Privacy risk

---

## Tips for Best Results

### Provide Clear Context

```
# Good
> Write a function that validates a credit card number using the Luhn algorithm

# Better
> Write a Python function called validate_credit_card that:
> - Takes a string as input
> - Returns True/False
> - Uses the Luhn algorithm
> - Includes type hints and docstring
```

### Break Down Complex Tasks

```
# Instead of
> Build a complete SaaS application

# Try
> Create the data models for users and subscriptions
> Add authentication endpoints
> Create the subscription billing logic
> Build the API routes
```

### Review AI Output

Always verify AI-generated code:

1. **Read the code** - Understand what it does
2. **Check for errors** - Syntax, type, logic
3. **Test thoroughly** - Run unit tests
4. **Consider edge cases** - Empty inputs, large data
5. **Security check** - No injection vulnerabilities

---

## Limitations and Caveats

### AI Hallucination

AI models may occasionally:
- Generate plausible but incorrect code
- Misremember function signatures
- Provide outdated information
- "Guess" when uncertain

**Mitigation**: Always review and test AI output.

### Context Loss

Long conversations may lose early context. **Mitigation**: Use sessions for project continuity.

### Model Quality

Results depend heavily on the underlying LLM. **Mitigation**: Use code-optimized models.

---

*Last updated: 2026-04-10*
