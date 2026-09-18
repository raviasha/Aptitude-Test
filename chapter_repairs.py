"""Vision-reviewed display repairs for question banks already in SQLite.

Fresh imports receive these corrections from the rebuilt chapter packages.  This
small runtime map keeps existing installations correct without deleting banks,
attempts, or results.
"""

CHAPTER_01_REPAIRS = {
    "ch01-q0041": {
        "question_text": "The difference between the squares of any two consecutive integers is equal to",
        "solution_steps": [
            "Let x and x + 1 be two consecutive integers.",
            "(x + 1)² − x² = [(x + 1) + x][(x + 1) − x] = 2x + 1, which is the sum of the two integers.",
        ],
    },
    "ch01-q0044": {
        "question_text": "If 0 < x < 1, which of the following is greatest? (Campus Recruitment, 2007)",
        "options": {"A": "x", "B": "x²", "C": "1/x", "D": "1/x²"},
        "solution_steps": [
            "0 < x < 1 ⇒ x² < x < 1 ...(i)",
            "⇒ 1/x² > 1/x > 1 > x > x² [using (i)]",
            "Hence, 1/x² is the greatest.",
        ],
    },
    "ch01-q0064": {
        "solution_steps": [
            "Clearly, 11 is a prime number that remains unchanged when its digits are reversed.",
            "Also, (11)² = 121. Hence, the square of such a number is 121.",
        ],
    },
    "ch01-q0116": {
        "solution_steps": [
            "1904 × 1904 = 1904² = (1900 + 4)².",
            "1900² + 4² + 2 × 1900 × 4 = 3610000 + 16 + 15200 = 3625216.",
        ],
    },
    "ch01-q0117": {
        "solution_steps": [
            "1397 × 1397 = 1397² = (1400 − 3)².",
            "1400² + 3² − 2 × 1400 × 3 = 1960000 + 9 − 8400 = 1951609.",
        ],
    },
    "ch01-q0118": {
        "solution_steps": [
            "107² + 93² = (100 + 7)² + (100 − 7)².",
            "Using (a + b)² + (a − b)² = 2(a² + b²), the value is 2(100² + 7²) = 2(10000 + 49) = 20098.",
        ],
    },
    "ch01-q0119": {
        "solution_steps": [
            "217² + 183² = (200 + 17)² + (200 − 17)².",
            "Using (a + b)² + (a − b)² = 2(a² + b²), the value is 2(200² + 17²) = 2(40000 + 289) = 80578.",
        ],
    },
    "ch01-q0120": {
        "solution_steps": [
            "106² − 94² = (106 + 94)(106 − 94).",
            "The value is 200 × 12 = 2400.",
        ],
    },
    "ch01-q0122": {
        "solution_steps": [
            "287² + 269² − 2 × 287 × 269 = (287 − 269)².",
            "Therefore, the value is 18² = 324.",
        ],
    },
    "ch01-q0123": {
        "question_text": "{(476 + 424)² − 4 × 476 × 424} = ?",
        "solution_steps": [
            "Use (a + b)² − 4ab = (a − b)², with a = 476 and b = 424.",
            "The expression is (476 − 424)² = 52² = 2704.",
        ],
    },
    "ch01-q0127": {
        "question_text": "(999)² − (998)² = ? (R.R.B., 2008)",
        "solution_steps": [
            "(999)² − (998)² = (999 + 998)(999 − 998).",
            "The value is 1997 × 1 = 1997.",
        ],
    },
    "ch01-q0128": {
        "question_text": "(80)² − (65)² + 81 = ?",
        "solution_steps": [
            "(80)² − (65)² + 81 = (80 + 65)(80 − 65) + 81 = (145 × 15) + 81 = 2175 + 81 = 2256.",
        ],
    },
    "ch01-q0172": {
        "question_text": "If x + y = 15 and xy = 56, what is the value of x² + y²? (L.I.C.A.D.O., 2007)",
        "solution_steps": [
            "x² + y² = (x + y)² − 2xy.",
            "Therefore, x² + y² = 15² − 2 × 56 = 225 − 112 = 113.",
        ],
    },
    "ch01-q0173": {
        "question_text": "Given that (1² + 2² + 3² + … + 20²) = 2870, the value of (2² + 4² + 6² + … + 40²) is",
        "options": {"A": "2870", "B": "5740", "C": "11480", "D": "28700"},
        "solution_steps": [
            "2² + 4² + 6² + … + 40² = (1 × 2)² + (2 × 2)² + (2 × 3)² + … + (2 × 20)².",
            "= 2² × (1² + 2² + 3² + … + 20²).",
            "= (4 × 2870) = 11480.",
        ],
    },
    "ch01-q0197": {
        "question_text": "The digit in the unit's place of [(251)⁹⁸ + (21)²⁹ − (106)¹⁰⁰ + (705)³⁵ − 16⁴ + 259] is",
        "solution_steps": [
            "The unit digits of the terms are respectively 1, 1, 6, 5, 6, and 9.",
            "Therefore, the required unit digit is the unit digit of 1 + 1 − 6 + 5 − 6 + 9 = 4.",
        ],
    },
    "ch01-q0235": {
        "solution_steps": [
            "For 37X3 to be divisible by 7, the textbook's divisibility test gives X = 0 or X = 7.",
        ],
    },
    "ch01-q0242": {
        "solution_steps": [
            "Let the consecutive integers be a and a + 1.",
            "(a + 1)² − a² = a² + 2a + 1 − a² = 2a + 1, which is the sum of the two integers.",
        ],
    },
    "ch01-q0255": {
        "solution_steps": [
            "Let the consecutive odd integers be (2m + 1) and (2m + 3).",
            "(2m + 3)² − (2m + 1)² = [(2m + 3) + (2m + 1)][(2m + 3) − (2m + 1)] = (4m + 4) × 2 = 8(m + 1).",
            "Therefore, the difference is always divisible by 8.",
        ],
    },
    "ch01-q0263": {
        "solution_steps": [
            "Let the odd natural number be (2n + 1).",
            "For n = 1, (2n + 1)² = 3² = 9, which leaves remainder 1 when divided by 8.",
            "For n = 2, (2n + 1)² = 5² = 25, which also leaves remainder 1; the same pattern continues.",
        ],
    },
    "ch01-q0265": {
        "solution_steps": [
            "Let the consecutive even integers be 2n and 2n + 2.",
            "(2n + 2)² − (2n)² = 4n² + 8n + 4 − 4n² = 4(2n + 1).",
            "Therefore, the difference is always divisible by 4.",
        ],
    },
    "ch01-q0266": {
        "solution_steps": [
            "Let the consecutive odd integers be (2m + 1) and (2m + 3).",
            "(2m + 3)² − (2m + 1)² = (4m + 4) × 2 = 8(m + 1).",
            "Therefore, the difference is divisible by 8.",
        ],
    },
    "ch01-q0305": {
        "solution_steps": [
            "Let the number be x and the quotient be q.",
            "x = 899q + 63 = (29 × 31q) + (29 × 2) + 5 = 29(31q + 2) + 5.",
            "Therefore, the number leaves remainder 5 when divided by 29.",
        ],
    },
    "ch01-q0319": {
        "solution_steps": [
            "Let the odd number be N = 2x + 1.",
            "N² = (2x + 1)² = 4x² + 4x + 1 = 4x(x + 1) + 1.",
            "Since one of x and x + 1 is even, 4x(x + 1) is divisible by 8. Hence, the remainder is 1.",
        ],
    },
    "ch01-q0325": {
        "question_text": "If (10¹² + 25)² − (10¹² − 25)² = 10ⁿ, what is n?",
        "solution_steps": [
            "Use (a + b)² − (a − b)² = 4ab, with a = 10¹² and b = 25.",
            "The value is 4 × 10¹² × 25 = 10¹² × 100 = 10¹² × 10² = 10¹⁴.",
            "Hence, n = 14.",
        ],
    },
    "ch01-q0340": {
        "question_text": "It is given that (2³² + 1) is exactly divisible by a certain number. Which option is also definitely divisible by the same number? (S.S.C., 2007)",
        "solution_steps": [
            "Let x = 2³². Then 2³² + 1 = x + 1.",
            "2⁹⁶ + 1 = (2³²)³ + 1 = x³ + 1 = (x + 1)(x² − x + 1).",
            "Therefore, every divisor of 2³² + 1 also divides 2⁹⁶ + 1.",
        ],
    },
    "ch01-q0341": {
        "question_text": "The number (2⁴⁸ − 1) is exactly divisible by two numbers between 60 and 70. The numbers are",
        "solution_steps": [
            "2⁴⁸ − 1 = (2⁶)⁸ − 1 = 64⁸ − 1.",
            "For even n, xⁿ − aⁿ is divisible by both x − a and x + a.",
            "Thus, 64⁸ − 1 is divisible by both 64 − 1 = 63 and 64 + 1 = 65.",
        ],
    },
    "ch01-q0343": {
        "question_text": "Let N = 55³ + 17³ − 72³. Then N is divisible by",
        "solution_steps": [
            "Let a = 55 and b = 17, so a + b = 72.",
            "N = a³ + b³ − (a + b)³ = −3ab(a + b) = −3 × 55 × 17 × 72.",
            "Therefore, N is divisible by both 3 and 17.",
        ],
    },
    "ch01-q0355": {
        "question_text": "What is the number of digits in the number (1024)⁴ × (125)¹¹?",
        "solution_steps": [
            "(1024)⁴ × (125)¹¹ = (2¹⁰)⁴ × (5³)¹¹ = 2⁴⁰ × 5³³.",
            "This is 2⁷ × (2³³ × 5³³) = 2⁷ × 10³³ = 128 × 10³³.",
            "The number has the digits 1, 2, 8 followed by thirty-three zeros, so it has 3 + 33 = 36 digits.",
        ],
    },
}
