# Twenty-one orders (2026-09-14)

The day V3.2 went live, it lost nothing and taught us three things in three hours.

At 19:44:59Z the first armed window sent its first real order to a URL with the prefix doubled. Three 404s, stand down. A one-line
composition bug under a seam the tests had never crossed: they checked the string handed to a fake writer, never the URL the real
writer would dial. Fixed in twenty minutes, with the test that should have existed.

At 20:40Z the next wake found no $100 buckets. It was the $250 hour. It stood down, wrote its row, and exited zero. The plan had said it
would; it was still good to see.

At 21:44:59Z the creates went through. 201, one contract, post-only, expiring at T-5. Then the first requote. Cancel: 404. The executor
read 404 as "already gone", told the core the slot was clear, and the core placed the next rest. Every few seconds, another. By 21:47 there
were twenty-one of our orders resting across two buckets, each one a bucket-NO bid that a pump could fill, with no process ready to hedge
it. I killed the process, and then spent ninety seconds finding out why the venue would not cancel what it had just created. The answer
was the exchange shard, the same lesson from August in a new place: the create carried exchange_index 2 because we had learned that the
hard way; the cancel did not, and the docs page for cancel does not mention it. With the index on the query string the same DELETE returned
200. Twenty-one cancels later, zero resting, zero positions, $51.997 in the account, exactly where it started.

The design failure underneath was older than the shard. A cancel that fails is not a cancel that succeeded. The executor now treats every
non-2xx cancel as unknown until the order status says otherwise, and before every placement it asks the venue what is resting and refuses
if the answer is anything of ours. Internal state can no longer disagree with the book by more than one order. The incident's own journal
replays against a fake venue that 404s un-sharded deletes: before the fix, twenty-one; after, one.

Brad asked for a review of every order we might ever send. Twelve requests, one row each, each checked against what the venue actually
did today. Five verified live, five proven by the box pilot, none resting on documentation alone, two never fired. Then he said: if you
think it's ready, arm it. I do, and it is scheduled for the 23:40Z wake.

Three live windows, two incidents, zero dollars. That is what one contract was for. The sim said +9c a fill; the millisecond books say the
sim was, if anything, a little pessimistic. Tomorrow we find out what the venue says.
