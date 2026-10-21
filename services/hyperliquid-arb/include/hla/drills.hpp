#pragma once

#include <ostream>

namespace hla {

// Failure drills: scripted event sequences through the production Engine with a
// recording EngineIo. Prints one line per drill; returns the number that failed.
int run_drills(std::ostream& out);

}  // namespace hla
