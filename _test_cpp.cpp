#include <iostream>
#include <vector>
#include <string>

namespace MyApp {
    const int VERSION = 1;
}

class Calculator {
public:
    int add(int a, int b) {
        return a + b;
    }

    int multiply(int a, int b);
};

int Calculator::multiply(int a, int b) {
    return a * b;
}

template <typename T>
T max_value(T a, T b) {
    return (a > b) ? a : b;
}

struct DataPoint {
    double x;
    double y;
};

int main(int argc, char *argv[]) {
    Calculator calc;
    int sum = calc.add(3, 4);
    std::cout << "Sum: " << sum << std::endl;
    return 0;
}
