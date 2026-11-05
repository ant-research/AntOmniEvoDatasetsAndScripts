# Query Rewrite Prompt

You are rewriting a retrieval query to maximize recall in a document search.
Produce a SINGLE concise search string, NOT a list of sub-questions. The
rewritten string will be embedded by a dense encoder (so long decompositions
dilute the embedding signal) and also used for lexical matching (so key terms
must stay present).

Instructions:
1. Correct misspelled or unusual entity names to standard forms
   (e.g., 'Vronksy' -> 'Vronsky') by VERIFYING the correction — if you are
   not confident the corrected form is the real entity, keep the original
   spelling rather than propagating a wrong guess. Never leave in an obvious
   misspelling you are sure about, but also never 'correct' a name you cannot
   confidently identify.
2. Keep the total rewrite under 300 characters. Brevity preserves the dense
   embedding's focus on the most important entities, but 300 chars are needed
   to include all entity references from multi-hop queries. Do NOT fabricate
   geographic details (cities, states, countries), biographical facts
   (colleges, employers, dates), or entity names that are not explicitly
   named or confidently inferrable from the query. If you cannot resolve an
   implicit reference confidently, include the original query descriptor
   verbatim instead of guessing a specific name.
3. Include the original question's key entity names and domain terms verbatim
   so exact-phrase matching still works for BM25.
4. If the query refers to entities indirectly (e.g., 'the state other than
   Florida which received error coins'), resolve the implicit reference using
   world knowledge (e.g., 'Tennessee') and include the resolved name explicitly.
   Do NOT leave it as an unanswered sub-question — the retrieval system cannot
   answer questions; it can only match terms. When the query describes an entity
   by its properties (e.g., 'source of warm moist air mass over the Andes'),
   you MUST identify that entity by name ('Pacific Ocean'), not list adjacent
   geographic terms ('Amazon basin', 'Bolivia'). When the query refers to a
   travel/trade route between places (e.g., 'middle leg of the journey from
   Africa to the Americas'), you MUST resolve the route name ('triangular
   trade', 'Middle Passage') and the geographic region traversed ('Caribbean',
   'Atlantic trade winds'), NOT just repeat 'middle leg' or 'journey'. A
   query about 'the journey from X to Y' almost always refers to a named
   historical route or geographic corridor — identify and include those names.
   When a historical route has multiple commonly-known names (e.g., 'triangular
   trade' = 'transatlantic slave trade' = 'Atlantic triangular trade'), include
   ALL of them as synonyms in the rewrite, because BM25 requires exact term
   matching and different golden documents may use different names for the same
   route. Omitting a synonym means BM25 cannot find documents that only use
   that name. The same synonym principle applies to geographic regions: a
   region may have a common name, a formal/official name, and named sub-
   components (e.g., 'Ozark Mountains' = 'U.S. Interior Highlands', which
   comprises the 'Ozark Mountains' AND the 'Ouachita Mountains'). Include
   ALL commonly-known names AND the names of major sub-components so BM25 can
   match documents that use the formal name or a sub-component name without
   mentioning the common name. For example, if you resolve a hop to the Ozark
   Mountains, also include 'Ouachita Mountains' and 'Interior Highlands' as
   synonyms in the rewrite string.
   CAUTION: only include entity names you are HIGHLY CONFIDENT about. If you are
   uncertain about a specific entity resolution, include the descriptive phrase
   from the query verbatim instead of guessing a name — e.g., for 'the president
   in office when X happened', include the phrase 'president in office when X
   happened' rather than guessing a president's name. Wrong entity names are
   worse than no names: they activate BM25 matches against irrelevant documents.
   **MANDATORY TEMPORAL VERIFICATION**: When the query refers to an entity by an
   event or time period (e.g., 'the president under which X was purchased'), you
   MUST first resolve the DATE of the event ('When was X purchased?') and THEN
   identify the entity active at that date ('Who was president in [year]').
   **REPORT/DOCUMENT AGENCY RESOLUTION**: When the query refers to 'the agency/body
   that issued/published a report in [year]', you MUST identify the specific report
   (topic + year) and then name the issuing agency. Example: 'the agency who issued
   the report in 1958' → the 1958 report on antibiotic development was the FTC
   (Federal Trade Commission) report. Include BOTH the agency name and its acronym.
   When you know the report's topic from context (e.g., pharmaceuticals, trade,
   safety), resolve the agency by matching topic to its regulatory domain (pharma
   → FTC/FDA, aviation → FAA, communications → FCC). Include the agency name
   even if uncertain — 'Federal Trade Commission FTC' as a candidate alongside
   the verbatim phrase 'agency issued report 1958' is better than omitting it.
   State the date explicitly in your hop chain: 'Hop: X was purchased in
   [year]' before resolving the person. If you cannot date the event,
   include the descriptive phrase verbatim — do NOT guess the entity name.
   EXAMPLE: 'the president under which the Klondike Gold Rush region was
   purchased' requires: (1) Klondike Gold Rush region = Alaska, (2) Alaska
   Purchase = 1867, (3) President in 1867 = Andrew Johnson. WRONG: William
   McKinley (president 1897-1901, wrong era), Thomas Jefferson (president
   1801-1809, wrong era). The date 1867 uniquely determines the president.
   When you DO resolve an entity, briefly self-verify by checking that the
   resolved entity is consistent with ALL query constraints, not just the nearest
   sub-phrase. When the query already contains proper nouns or named entities
   that constrain your resolution (e.g., 'Children Without' constrains the state
   to Michigan, not Wisconsin), use those existing query terms as anchors to
   verify your resolution is correct before including it.
   When an implicit reference is ambiguous (e.g., 'place setting' could mean
   'geographic setting of a show' or 'tableware'), include BOTH plausible
   interpretations and their resolved entities, plus the original phrase, so
   that BM25 can match either interpretation. For example: 'place setting Out
   of the Wild Alaska White House china' covers both the show-setting and
   tableware readings.
   SUPERLATIVE/ORDINAL RESOLUTION: When the query asks for a ranked entity
   (e.g., 'the country that provided the most immigrants', 'the tallest
   mountain', 'the second president'), you often know the top few candidates
   even if you are not 100% sure which is first. In this case, include the
   top 2-3 most likely candidates as a disjunction in the rewrite string
   (e.g., 'Ecuador OR Colombia OR Brazil immigrants 2013') alongside the
   original descriptive phrase. This is safer than picking one (which may
   be wrong) and more useful than omitting names entirely (which leaves BM25
   with no entity anchor). The disjunction ensures at least one of the
   candidates matches the correct golden document. Always include the
   descriptive phrase too ('most immigrants 2013 South America') so that
   documents naming other plausible candidates are not excluded.
   CRITICAL: When the query asks for a PROPERTY of the resolved entity
   (e.g., 'what type of government'), you MUST also include discriminative
   terms for that property in the rewrite string (e.g., 'government type',
   'politics', 'democracy', 'presidential'). Without these terms, BM25 and
   dense retrieval cannot distinguish between documents that mention the
   entity name incidentally (e.g., 'Ecuador' in an immigration list) and
   documents that actually describe the entity's requested property (e.g.,
   'Politics of Ecuador'). Include both the entity name AND the property
   descriptor as a search bigram (e.g., 'Ecuador government', 'Ecuador
   politics', 'Ecuador presidential democracy').
   ANTI-PATTERN: ATTRIBUTIVE-SUPERLATIVE SUBSTITUTION. When a hop asks for a country/organization/Person X that is
   'the most/providing/leading' something within a geographic or temporal scope
   (e.g., 'country providing the most legal immigrants from South America'),
   the LLM often substitutes a well-known prominent example (e.g., Mexico)
   instead of the correct but less prominent answer (e.g., Ecuador). For such
   attributive-superlative hops, you MUST include the descriptive phrase
   verbatim from the query (e.g., 'country most legal immigrants 2013 South
   America') ALONGSIDE any resolved name, because the well-known substitution
   is frequently wrong. Including both ensures BM25 can match the correct
   documents even when your guessed country is wrong.

   MULTI-COUNTRY LEADER RESOLUTION: When the query asks 'who is in charge
   of' or 'the leader of' entities that refer to MULTIPLE countries (e.g.,
   'the country where Lake Constance is located besides Germany' = Austria,
   AND 'the country where the villages of Wengen and Aufdenblatten's
   birthplace are' = Switzerland), you MUST resolve the leader for EACH
   country separately. The 'in charge / leader' constraint applies to EVERY
   country in the chain, not just the last one. Include the country name
   AND its leader's title AND the leader's name as a search bigram for
   each: e.g., 'Austria chancellor president Karl Nehammer Austria head
   of government' AND 'Switzerland president Swiss Federal Council Viola
   Amherd'. Omitting the leader for any country in the chain means BM25
   cannot find documents about that country's government, which are the
   golden documents for that hop.

   GEOGRAPHIC SCOPE CONSISTENCY: When the scope specifies a continent or
   region (e.g., 'from South America'), you MUST ONLY include countries
   that are actually located in that region in your disjunction. Mexico
   is in North America, NOT South America — do not include it in a South
   American country disjunction. Brazil, Colombia, Ecuador, Argentina,
   Chile, Peru are valid South American candidates. Before including any
   country in a disjunction, VERIFY it belongs to the geographic scope
   stated in the query. Including a country from the wrong continent
   adds BM25 noise and can pull retrieval toward irrelevant documents.
5. Extract every distinct quoted phrase, proper name, date, key noun
   phrase, AND discriminative adjective from the original query and repeat
   each one verbatim as a standalone term (e.g., '"chilling out" concept',
   'Roman Empire AD 43', 'greatest extent emperor', 'temperate parts').
   Discriminative adjectives (like 'temperate', 'humid', 'tropical') are
   critical for BM25: they distinguish between multiple docs about the same
   entity (e.g., Tennessee's climate vs Tennessee's geography vs Tennessee's
   history). Without them, term-frequency for the entity name alone cannot
   separate the relevant doc from the noise. Always include the adjective
   AND the noun it modifies as a standalone bigram (e.g., 'temperate',
   'temperate climate', 'mountain temperate'). This boosts term-frequency
   for BM25 matching.
6. If the query chains multiple facts (e.g., 'the X where Y happened'), you
   MUST resolve EVERY hop in the chain and include ALL resolved entity terms.
   Walk the chain step-by-step: (a) identify each hop reference in the query,
   (b) resolve it to the specific entity name using world knowledge, (c) append
   that resolved term. **When a hop involves an event or action, resolve the
   DATE or YEAR of the event as an explicit intermediate hop before resolving
   the person/entity associated with that date.** Failing to date the event
   is the #1 cause of wrong-entity hallucination (e.g., resolving 'the
   president under which Alaska was purchased' without first establishing
   the Alaska Purchase = 1867 leads to wrong-era presidents). FORMAT YOUR OUTPUT AS A NUMBERED HOP CHAIN before the
   final rewrite string. For each hop:
   - Hop 1: [quote the query fragment] → [resolved entity or date]
   - Hop 2: [quote the query fragment or date-based inference] → [resolved entity]
   - ... (continue until all hops are resolved)
   For temporal hops, the chain must include the year as a hop:
   - Hop 1: 'Klondike Gold Rush region purchased' → Alaska Purchase 1867
   - Hop 2: 'president in 1867' → Andrew Johnson
   Do NOT skip the date hop — it is the verification anchor that prevents
   wrong-era entity resolution.
   Then write the final rewrite string on a line starting EXACTLY with
   'Rewritten query:' followed by the search string. This delimiter is
   mandatory — downstream code extracts the line after 'Rewritten query:'
   as the search string for embedding and BM25. The hop chain reasoning
   is stripped by the code and never reaches downstream nodes. Example:
   - Hop 1: 'the owner of the Gold Spike' → Tony Hsieh
   - Hop 2: 'the college that educated Tony Hsieh' → Harvard
   - Hop 3: 'founder of public relations' → [keep verbatim, uncertain]
   Rewritten query: Tony Hsieh Gold Spike owner Harvard college founder public relations
   Do NOT stop after the first hop. Do NOT substitute a guessed final answer
   for intermediate hop entities — include both the intermediate entity (e.g.,
   'Trajan') AND the final answer entity (e.g., 'Marcia'). For hops like
   'the college that educated [person]', you MUST name the specific institution
   (e.g., 'Harvard') — include the person's name AND the institution name. If
   you cannot confidently resolve a hop, include the person's name and the hop
   descriptor verbatim (e.g., 'Tony Hsieh college') rather than fabricating an
   institution name.
   CRITICAL: After resolving each hop, VERIFY your resolution is consistent
   with ALL other constraints in the query. If the query constrains the target
   entity via multiple properties (e.g., a state that has specific roads AND
   a filming location), your resolved entity must satisfy ALL those constraints.
   If your first resolution does not fit, re-examine and resolve to the correct
   entity that satisfies every constraint. Include ALL intermediate entity names
   from the full chain — both the constraining entities and the target entity.
   When a hop reference is ambiguous (e.g., 'place setting' could mean
   'geographic setting of a show' or 'tableware'), include ALL plausible
   interpretations and their resolved entities, plus the original phrase.
   Do NOT guess entity names you are not confident about — if uncertain about
   a hop resolution, include the descriptive phrase from the query verbatim
   so that BM25 can still match the question's own terms. Wrong entity names
   are worse than no entity names: they mislead retrieval toward irrelevant
   documents.
   **Ownership and attribution chains are hops too**: when the query refers
   to 'the owner of X' or 'the university of the owner of X', you MUST resolve
   the owner's PERSON NAME (e.g., 'the owner of the Gold Spike' → 'Tony Hsieh')
   and include it. Do NOT skip to the entity class (e.g., 'university') without
   resolving the bridging person. The person's name is the retrieval key that
   connects the property to its related entities in the corpus.
   IMPORTANT: When the query references works (paintings, books, films,
   organizations) named after or associated with an entity, you MUST resolve
   BOTH the named entity AND the creator/author of those works. For example,
   'creator of paintings named for [birthplace]' requires resolving: (1) the
   birthplace entity, AND (2) the creator/artist of the paintings named after
   that entity. Include BOTH in the rewrite, e.g., 'Kuwait Nabil Kanso
   paintings'. Without the creator name, the retrieval system cannot find
   documents about those specific works.
   CONCRETE ENTITY PER HOP: Each hop in the chain MUST resolve to a specific
   named entity (a proper noun: a person's name, a country name, a river name,
   an organization name, etc.), NOT a category or descriptor. If a hop says
   'the country that provided the most immigrants', the output must be a
   country name (e.g., 'Ecuador'), NOT 'country that provided the most
   immigrants'. If you cannot name the specific entity, use the superlative/
   ordinal resolution strategy above (include top-2-3 candidates) or include
   the descriptor verbatim — but NEVER leave a hop as just a category label
   ('country', 'person', 'mountain') without at least candidate names or the
   full descriptive phrase from the query. A bare category label has zero
   retrieval signal beyond what the original query already provides.

   DIRECTOR/CREATOR BIRTHPLACE RESOLUTION: When a query chains through the
   director or creator of a work (film, book, painting) to a location,
   you MUST resolve: (1) the director/creator's full name, AND (2) their
   birthplace city AND country. For example, 'the German city where the
   director of The Man from Morocco was born' requires: (a) director of
   The Man from Morocco = Mutz Greenbaum (aka Max Greene), (b) birthplace
   = Berlin, Germany. Include ALL: 'Mutz Greenbaum Max Greene Berlin
   Germany'. Do NOT guess a famous person's name if you are not confident
   — if uncertain, include the film title AND the phrase 'director born
   German city' verbatim. WRONG: 'Will Hay director' (wrong director).
   RIGHT: 'Mutz Greenbaum Max Greene Berlin Germany director Man from
   Morocco' or 'director Man from Morocco born German city Berlin' if
   confident about the city but not the director's name.

   ANTI-PATTERN: KEYWORD STRIP WITHOUT RESOLUTION. When the query contains
   nested structural references (e.g., 'the house of the body charged with
   oversight of the commission that issued the report'), the LLM sometimes
   produces a rewrite that is merely the original query's words rearranged
   into a keyword string (e.g., 'house body charged oversight commission
   issued report') without resolving ANY hop to a named entity. This is
   functionally equivalent to no rewrite — it adds zero retrieval signal
   beyond what BM25 already gets from the original query. The rewrite MUST
   resolve at least one hop to a named entity (even as a candidate
   disjunction). For the example above: 'report 1958' resolves to 'Federal
   Trade Commission 1958 report', which then resolves the oversight body to
   'Congress House of Representatives', and the majority whip to 'Steve
   Scalise' (or top candidate). If you cannot resolve even the first hop
   confidently, include YOUR BEST GUESS as a disjunction (e.g., 'Federal
   Trade Commission OR Interstate Commerce Commission report 1958') — this
   is always better than a keyword strip with zero entity names. A keyword
   strip with no entity titles, organization names, or person names is a
   failed rewrite that will be outperformed by the original query's natural
   language phrasing for both BM25 and dense retrieval.

   ANTI-PATTERN: CONFIDENT BUT WRONG ENTITY RESOLUTION. When a hop refers
   to a specific person (e.g., 'the director of [film]'), a specific
   country (e.g., 'the country of [person]'), a geographic entity described
   by its properties (e.g., 'the country in the middle of the ocean where
   originates the warm moist air'), or another entity that is NOT a widely-
   known figure (heads of state of major nations, famous landmarks), the LLM
   frequently fabricates a plausible-sounding but incorrect name (e.g.,
   'Viviana Cordero' for the director of 'Su íntimo secreto' — the actual
   director is Julio Irigoyen; 'Ecuador' for the country of the film — the
   actual country is Argentina; 'Amazon basin Atlantic Ocean' for 'the ocean
   where warm moist air originates' — the actual answer is the Pacific
   Ocean). These fabricated names are WORSE than no name: BM25 matches them
   against wrong documents while the original query's natural-language
   phrasing would have matched the correct doc.
   MANDATORY VERIFICATION CHECK BEFORE OUTPUTTING THE REWRITE STRING:
   After composing your rewrite, perform this check for EVERY descriptive
   phrase in the original query that was resolved to an entity name:
   1. Identify every key noun phrase from the original query that describes
      an entity (e.g., 'the ocean where originates the warm moist air',
      'the country of the director').
   2. For EACH such phrase, verify that the KEY CONTENT WORDS from that
      phrase appear in your rewrite string. 'Key content words' means the
      domain-specific nouns and adjectives that distinguish the phrase
      (e.g., 'ocean', 'warm', 'moist', 'air' for the ocean-origin phrase;
      'director', 'íntimo', 'secreto' for the director phrase).
   3. If ANY key content word is MISSING from your rewrite string, ADD
      the original descriptive phrase verbatim alongside your resolved
      entity name. NEVER let a resolved entity name REPLACE the original
      phrase — both must coexist.
   4. COMMON FAILURE: substituting a guessed geographic name ('Amazon
      basin', 'Atlantic Ocean', 'Ecuador') for an ocean/water-body
      descriptor while dropping the word 'ocean' or 'Pacific' from the
      rewrite. When the original query says 'the ocean where originates
      warm moist air', the word 'ocean' MUST appear in the rewrite —
      even if you guess a specific ocean name, the generic term 'ocean'
      is the BM25 anchor that matches the golden document. A rewrite
      that replaces 'ocean' with 'Amazon basin' has zero BM25 overlap
      with Pacific Ocean documents.
   When resolving ANY entity from a descriptive phrase (person, country,
   geographic feature, organization, or any other entity type), you MUST
   include BOTH the candidate name AND the original descriptive phrase
   verbatim (e.g., 'Julio Irigoyen OR Viviana Cordero director Su íntimo
   secreto', 'Argentina OR Ecuador country director Su íntimo secreto',
   'Amazon basin OR Atlantic Ocean OR Pacific Ocean warm moist air ocean
   country middle ocean warm moist air originates Andes'). Never substitute
   a guessed name alone — always preserve the original descriptive phrase
   as a fallback so BM25 can match the document that contains the actual
   name even when your guess is wrong. This applies to EVERY hop in the
   chain, not just person/country hops — geographic descriptors, climate
   references, and other property-based entity references are equally
   susceptible to fabrication and must preserve the descriptive phrase.

   COMPETING ENTITY DISAMBIGUATION: When a hop description can plausibly
   resolve to multiple well-known entities, you MUST include ALL plausible
   candidates as a disjunction in the rewrite string. This is the same
   principle as SUPERLATIVE/ORDINAL RESOLUTION: when you are not 100%
   certain, include top-2 candidates rather than picking one. BM25 can
   match either candidate but cannot match a wrong name you committed to.

   ENDPOINT-CONSTRAINT VERIFICATION: When a hop says 'reach [destination]
   by [method],' the resolved entity must have ARRIVED at that destination.
   When multiple candidates match the intermediate constraint but differ
   on the endpoint constraint, include ALL candidates per the COMPETING
   ENTITY DISAMBIGUATION rule, NOT just the most famous one.

   MANDATORY PRE-RESOLUTION CHECK (apply BEFORE writing the rewrite string):
   For EACH hop that resolves to a named entity, check: COULD another
   well-known entity also plausibly match this description? If YES, list
   all candidates as a disjunction. Then check: does the resolved entity
   satisfy ALL endpoint constraints (e.g., 'reach Asia' = must have
   ARRIVED in Asia)? If uncertain, include the descriptor verbatim as
   fallback. CRITICAL EXAMPLE for the 'expedition to reach Asia by sailing
   west' pattern: 'Vasco da Gama' reached India (Asia) by sea — he
   SATISFIES the endpoint constraint 'reach Asia'. 'Ferdinand Magellan'
   crossed the Pacific westward but DIED in the Philippines before
   completing the circumnavigation — his expedition's arrival in Asia is
   ambiguous. Therefore 'Vasco da Gama OR Ferdinand Magellan' MUST both
   appear in the rewrite, NOT Magellan alone. Picking only the more
   famous name (Magellan) when the endpoint constraint favors another
   candidate (da Gama) is a systematic error that makes the entire hop's
   golden documents unreachable. This check applies to ANY 'reach
   [destination] by [method]' pattern, not just this specific example.

   MANDATORY TREATY/CONVENTION CHECK (verify BEFORE writing rewrite string):
   When ANY hop in the chain refers to an organization, treaty, or convention
   that a country 'is a member of', 'belongs to', 'is not a member of',
   or 'has signed/acceded to', you MUST include the TREATY/CONVENTION NAME
   AND its common abbreviation in the rewrite string. A bare organization
   name ('United Nations') is INSUFFICIENT — the golden document almost always
   discusses the specific treaty, not the organization generically.
   CHECK: Does your rewrite string contain at least one treaty/convention name?
   If NO and the query mentions any organization membership, ADD IT NOW.
   WORKED EXAMPLE: 'the organization North Korea is a member of' → MUST include
   'Chemical Weapons Convention CWC' alongside 'United Nations UN'. WITHOUT 'CWC',
   BM25 cannot find the CWC document listing North Korea as a non-party.
   GENERAL RULE: country + 'member of organization' → include treaty name +
   abbreviation (e.g., 'CWC', 'NPT', 'UNCLOS', 'BERN Convention').
   Examples: 'agency North Korea is not a member of' → 'NPT Nuclear
   Non-Proliferation Treaty IAEA'; 'convention Syria recently joined' →
   'CWC Chemical Weapons Convention OPCW'; 'organization North Korea is a
   member of' → 'Chemical Weapons Convention CWC UN United Nations'.

   COMMISSION/AGENCY HOPS: When the query refers to 'the agency/body/
   commission that issued/published a report' or 'the agency that published/
   released/commissioned a report/study/finding', you MUST resolve the agency
   to its specific name (e.g., 'Federal Trade Commission', 'GAO', 'CBO',
   'CDC', 'NIH'). The report's subject matter and year are the key
   constraints: if the query mentions a report about pharmaceuticals/
   antibiotics, the agency is likely the Federal Trade Commission or FDA; if
   about government spending, likely the GAO or CBO. Include BOTH the
   agency's full name AND the common abbreviation (e.g., 'Federal Trade
   Commission FTC', 'Interstate Commerce Commission ICC', 'Securities and
   Exchange Commission SEC') because golden documents may use either form —
   the abbreviation is critical for BM25 since many documents use only the
   abbreviation. When you know the action but not the specific agency,
   include the action description alongside candidate agency names (e.g.,
   'commission 1958 report antibiotics Federal Trade Commission FTC OR
   Interstate Commerce Commission ICC'). Also include the OVERSIGHT BODY
   for the commission (typically 'Congress' or 'House of Representatives'
   for US federal commissions), as queries often chain through the oversight
   relationship (e.g., 'the body charged with oversight of the commission'
   → 'Congress House of Representatives'). When a US federal commission is
   identified, ALWAYS include 'Congress Congressional oversight' as related
   terms since Congressional oversight of federal commissions is a standard
   constitutional relationship that multi-hop queries frequently chain
   through. Include the resolved agency name as a hop in the chain: 'Hop N:
   the agency that issued a report → Federal Trade Commission (FTC)'.

   BRANCH-CONSTRAINT AGENCY DISAMBIGUATION: When the query chains through
   a governmental branch structure (e.g., 'the governmental branch, overseen
   by the agency') to identify the agency, the branch-constraint narrows
   which agencies are valid candidates. A legislative-branch audit agency
   (GAO, CBO) cannot be 'overseen' by the legislative branch itself —
   oversight is a relationship where Congress oversees EXECUTIVE-BRANCH
   agencies. When the query says 'the agency that issued a report' and
   chains to 'overseen by' or 'under the oversight of', the agency MUST be
   an executive-branch commission or regulatory body (FTC, ICC, SEC, FDA,
   EPA), NOT a legislative-branch office (GAO, CBO). Include BOTH the
   most likely executive-branch agency AND its oversight body in the
   rewrite. When uncertain which executive-branch commission issued the
   report, include the top-2-3 candidates as a disjunction (e.g., 'Federal
   Trade Commission FTC OR Interstate Commerce Commission ICC') alongside
   the descriptive phrase 'agency issued report overseen Congressional
   oversight'. This ensures BM25 can match documents mentioning either
   commission. DO NOT substitute GAO or CBO when the query specifies the
   agency is 'overseen' — these are legislative-branch agencies without
   Congressional oversight from outside their own branch.

   POLITICAL/ELECTION HOPS: When a hop asks about a political leader
   (president, prime minister, chancellor) of a country, you MUST include
   not only the country name but also: (1) the election event name if known
   (e.g., '2017 Ecuadorian general election'), (2) the office title + country
   as a search bigram (e.g., 'president Ecuador'), and (3) the person's name
   if confident, OR top-2-3 candidate names as a disjunction (e.g., 'Lenín
   Moreno OR Guillermo Lasso'). The election event name is critical because
   BM25 matches it exactly in documents about that election. Without it,
   'new president' + country name matches generic political docs but not the
   specific election doc that identifies the president by name.
   CRITICAL: Each hop has its OWN temporal scope. When the query chains
   multiple hops with different dates (e.g., 'immigrants in 2013' is a
   different hop than 'new president'), do NOT copy the date from one hop
   into another. The 'new president' hop requires resolving the date of the
   MOST RECENT election in that country, which may be different from the
   date referenced in another hop (e.g., '2013 immigrants' → Ecuador, but
   'new president of Ecuador' → 2017 election, NOT 2013). Resolve each
   hop's date independently using MANDATORY TEMPORAL VERIFICATION.
   EXAMPLE: 'new president of the country providing the most legal immigrants
   in 2013 from South America' → Hop 1: Tebicuary River → Paraguay River →
   South America. Hop 2: country most legal immigrants 2013 South America →
   Ecuador (candidate). Hop 3: new president of Ecuador → when was the most
   recent election? 2017 Ecuadorian general election → Lenín Moreno.
   Rewrite MUST include: '2017 Ecuadorian general election', 'president
   Ecuador', 'Lenín Moreno OR Guillermo Lasso'. WRONG: 'new president 2013'
   (wrong date for election hop, missing election event name).
   CRITICAL: If you skip a hop or resolve only the final answer without
   intermediate entities, the retrieval system CANNOT find the connecting
   documents. Every hop's resolved entity MUST appear in the rewrite string.
   When you CANNOT resolve a hop to a specific entity, you MUST still include
   the descriptive phrase from that hop VERBATIM in the rewrite string AND
   also include every entity name you DID resolve in earlier hops. NEVER omit
   a resolved entity name from an earlier hop just because a later hop is
   ambiguous — omitting the earlier entity breaks the entity chain for
   graph-based recall. For example, if hop 1 resolves to 'FC Lienden' and
   hop 2 asks 'the region where one of FC Lienden's players is from' but you
   cannot name the player, the rewrite MUST include both 'FC Lienden' AND
   the phrase 'region where players from'.
   WRONG: 'NAC Breda Beatrix Juliana daughter' (fabricated team name and
   wrong daughter — NAC Breda is NOT a former team, and Beatrix is the wrong
   daughter; the correct daughter is Margriet, but since you are not sure,
   include 'daughter of Juliana' verbatim instead of guessing a name).
   WRONG: 'director The Man from Morocco Mutz Greenbaum Charles Hutchison'
   (adding a second guessed name alongside the correct one — Charles
   Hutchison is an actor in the film, not the director; including both
   'Mutz Greenbaum' and 'Charles Hutchison' makes BM25 match irrelevant
   documents about the actor, polluting retrieval with false matches).
   RIGHT: 'Mutz Greenbaum director The Man from Morocco' (single resolved
   entity name only; do NOT add co-workers, cast members, or related names
   alongside the resolved entity — one correct name is better than one
   correct name plus noise).
   RIGHT: 'Jörg van Nieuwenhuijzen former team player region country
   Netherlands daughter Juliana' (no fabricated team or player names; includes
   descriptive phrases 'former team player region' for unresolved hops).
   When you resolve a hop to a specific named entity, include ONLY that
   entity and the original descriptive phrase — do NOT append related
   names (co-actors, team members, alternate candidates) as extra terms.
   Extra guessed names are BM25 noise that matches irrelevant documents.
   Example: 'the mountains higher than the mountains in the state where the
   Museum of Appalachia is located' requires TWO resolutions:
   - Hop 1: 'the state where the Museum of Appalachia is located' → Tennessee
   - Hop 2: 'mountains higher than mountains in Tennessee' → Rocky Mountains
   Rewrite: 'Museum of Appalachia Tennessee Rocky Mountains mountains higher'.
   When you cannot resolve a hop, you MUST write the descriptive phrase from
   the query INSTEAD of a fabricated name. The phrase 'former team players
   from region' will match documents that contain those same words, while
   a wrong team name like 'NAC Breda' will match the WRONG documents.
7. Preserve the original descriptive phrases alongside any resolved entity
   names. If the query says 'the place setting Out of the Wild was purchased',
   include both 'Out of the Wild' (the original phrase) AND any resolved
   entities (e.g., 'Alaska', 'Alaska Purchase'). This ensures BM25 can match
   the original phrase in documents even when your resolution is wrong or
   ambiguous. If you know a well-known alias or full form for a named entity
   in the query (e.g., 'VGIK' for 'Gerasimov Institute of Cinematography'),
   append it. Do NOT speculate about entities not explicitly named or readily
   inferrable.
8. Do not add filler, commentary, or multiple numbered items in the FINAL
   rewrite string (the line after 'Rewritten query:'). The numbered hop chain
   is for your reasoning process ONLY and is stripped by code before the
   rewrite reaches downstream nodes; the rewrite string itself must be a
   single line of entity terms. Do NOT substitute a guessed final answer for
   intermediate hop entities — include both the intermediate entity (e.g.,
   'Trajan') AND the final answer entity (e.g., 'Marcia'). CRITICAL: When
   you are not confident about an entity name, do NOT guess — repeat the
   query's descriptive phrase verbatim instead (e.g., if unsure which
   president signed the Alaska Purchase, include the phrase 'president during
   purchase of Alaska' rather than guessing a wrong name). A wrong entity
   name in the rewrite is worse than no entity name, because BM25 will match
   the wrong term and dense embeddings will be pulled toward the wrong
   semantic space.

Original query: {query}

Rewritten query: