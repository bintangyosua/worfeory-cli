use std::collections::HashMap;
use std::env;
use std::sync::LazyLock;

// Embed the JSON data directly into the binary
static WORDLIST_RAW: &str = include_str!("../data/wordlist.json");
static FREQMAP_RAW: &str = include_str!("../data/freqmap.json");

static WORDLIST: LazyLock<Vec<String>> = LazyLock::new(|| {
    serde_json::from_str(WORDLIST_RAW).expect("Failed to parse wordlist.json")
});

static FREQMAP: LazyLock<HashMap<String, f64>> = LazyLock::new(|| {
    serde_json::from_str(FREQMAP_RAW).expect("Failed to parse freqmap.json")
});

/// Return the pattern (as digits) given a guess and an answer.
/// 0 = absent, 1 = present (wrong position), 2 = correct
/// Handles duplicate letters correctly.
fn check_pattern(guess: &[u8; 5], answer: &[u8; 5]) -> [u8; 5] {
    let mut pattern = [0u8; 5];
    let mut remaining = [0u8; 5];
    remaining.copy_from_slice(answer);

    // Pass 1: mark correct (green)
    for i in 0..5 {
        if guess[i] == remaining[i] {
            pattern[i] = 2;
            remaining[i] = b'.';
        }
    }

    // Pass 2: mark present (yellow)
    for i in 0..5 {
        if pattern[i] != 2 {
            if let Some(idx) = remaining.iter().position(|&c| c == guess[i]) {
                pattern[i] = 1;
                remaining[idx] = b'.';
            }
        }
    }

    pattern
}

/// Convert a pattern array to a 5-digit string like "01020"
fn pattern_to_string(p: &[u8; 5]) -> String {
    p.iter().map(|&d| (d + b'0') as char).collect()
}

/// Compute entropy of a guess against a set of remaining candidates.
fn entropy(candidates: &[&String], guess: &[u8; 5]) -> f64 {
    let mut bucket: HashMap<[u8; 5], f64> = HashMap::new();
    let mut total: f64 = 0.0;

    for word in candidates {
        let wb: [u8; 5] = word.as_bytes()[..5].try_into().unwrap();
        let pat = check_pattern(guess, &wb);
        let freq = FREQMAP.get(word.as_str()).copied().unwrap_or(1e-10);
        *bucket.entry(pat).or_insert(0.0) += freq;
        total += freq;
    }

    if total == 0.0 {
        return 0.0;
    }

    let mut h: f64 = 0.0;
    for &prob in bucket.values() {
        let p = prob / total;
        if p > 0.0 {
            h += p * (1.0 / p).log2();
        }
    }
    h
}

/// Filter candidates to only those consistent with a given guess and pattern.
fn filter(candidates: &[&String], guess: &[u8; 5], pattern: &[u8; 5]) -> Vec<String> {
    candidates
        .iter()
        .filter(|word| {
            let wb: [u8; 5] = word.as_bytes()[..5].try_into().unwrap();
            check_pattern(guess, &wb) == *pattern
        })
        .map(|w| (*w).clone())
        .collect()
}

/// Parse a 5-digit pattern string "01020" into [u8; 5]
fn parse_pattern(s: &str) -> [u8; 5] {
    let bytes = s.as_bytes();
    let mut p = [0u8; 5];
    for i in 0..5 {
        p[i] = bytes[i] - b'0';
    }
    p
}

fn main() {
    let args: Vec<String> = env::args().collect();

    // Usage: solver.exe <guess1> <result1> [<guess2> <result2> ...]
    // Must have an even number of arguments (pairs of guess + result)
    if args.len() < 3 || (args.len() - 1) % 2 != 0 {
        eprintln!("Usage: solver.exe <guess> <result> [<guess2> <result2> ...]");
        eprintln!("Example: solver.exe crane 01020");
        std::process::exit(1);
    }

    // Start with full wordlist
    let mut remaining: Vec<String> = WORDLIST.clone();

    // Apply all guess/result pairs to filter candidates
    let num_pairs = (args.len() - 1) / 2;
    for i in 0..num_pairs {
        let guess_str = args[1 + i * 2].to_lowercase();
        let result_str = &args[2 + i * 2];

        if guess_str.len() != 5 || result_str.len() != 5 {
            eprintln!("Error: guess and result must be exactly 5 characters");
            std::process::exit(1);
        }

        let guess: [u8; 5] = guess_str.as_bytes()[..5].try_into().unwrap();
        let pattern = parse_pattern(result_str);

        let refs: Vec<&String> = remaining.iter().collect();
        remaining = filter(&refs, &guess, &pattern);
    }

    // Pick the best next guess
    if remaining.is_empty() {
        eprintln!("No candidates remaining");
        std::process::exit(1);
    }

    if remaining.len() == 1 {
        println!("{}", remaining[0]);
        return;
    }

    if remaining.len() == 2 {
        // Just pick the more frequent one
        let f0 = FREQMAP.get(remaining[0].as_str()).copied().unwrap_or(0.0);
        let f1 = FREQMAP.get(remaining[1].as_str()).copied().unwrap_or(0.0);
        if f0 >= f1 {
            println!("{}", remaining[0]);
        } else {
            println!("{}", remaining[1]);
        }
        return;
    }

    // Score all words in the FULL wordlist by entropy against remaining candidates
    let refs: Vec<&String> = remaining.iter().collect();

    let mut best_word = String::new();
    let mut best_score: f64 = -1.0;

    for word in WORDLIST.iter() {
        let wb: [u8; 5] = word.as_bytes()[..5].try_into().unwrap();
        let score = entropy(&refs, &wb);
        if score > best_score {
            best_score = score;
            best_word = word.clone();
        }
    }

    println!("{}", best_word);
}
